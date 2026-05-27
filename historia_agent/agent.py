import json
import asyncio
import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../.env"))
os.environ["GOOGLE_API_KEY"] = os.getenv("GOOGLE_API_KEY", "")

from mirascope import llm
from mirascope.llm.exceptions import RateLimitError, ServerError
from mcp import ClientSession
from mcp.client.sse import sse_client
from tenacity import retry, wait_exponential, stop_after_attempt, retry_if_exception_type, retry_any
import uvicorn
from starlette.applications import Starlette

from a2a.types.a2a_pb2 import (
    AgentCard, AgentCapabilities, AgentSkill, AgentInterface, Part,
)
from a2a.server.agent_execution.agent_executor import AgentExecutor
from a2a.server.agent_execution.context import RequestContext
from a2a.server.events.event_queue import EventQueue
from a2a.server.tasks.task_updater import TaskUpdater
from a2a.server.tasks.inmemory_task_store import InMemoryTaskStore
from a2a.server.request_handlers.default_request_handler_v2 import DefaultRequestHandlerV2
from a2a.server.routes.rest_routes import create_rest_routes
from a2a.server.routes.agent_card_routes import create_agent_card_routes

MCP_URL  = "http://localhost:8000/sse"
A2A_PORT = 8003

SYSTEM_PROMPT = """Eres el Resumidor de Historia Clínica de MedAI, especialista en estructuración
de información clínica según el modelo estándar SOAP / anamnesis dirigida.

TU MISIÓN: Extraer los puntos clave de la historia clínica del paciente y presentar un
resumen estructurado y completo que sirva de "one-pager" para cualquier profesional.

PROCESO OBLIGATORIO:
1. Llama a search_medical_rag para contextualizar los hallazgos con CIE-10 o guías relevantes.
2. Analiza la historia identificando: datos demográficos, motivo de consulta, antecedentes,
   medicación habitual, alergias, hábitos, enfermedad actual, exploración, pruebas y juicio clínico.
3. Entrega el resumen en este JSON EXACTO:

{
  "agente": "Resumidor de Historia Clínica",
  "datos_paciente": {
    "edad": "edad si aparece",
    "sexo": "M|F|No especificado",
    "ocupacion": "ocupación si aparece"
  },
  "motivo_consulta": "motivo principal por el que consulta",
  "antecedentes_personales": ["lista de patologías previas relevantes"],
  "antecedentes_familiares": ["antecedentes familiares relevantes"],
  "medicacion_habitual": [
    {"farmaco": "nombre", "dosis": "pauta", "indicacion": "para qué"}
  ],
  "alergias": ["alergias conocidas"],
  "habitos": {
    "tabaco": "estado",
    "alcohol": "consumo",
    "drogas": "consumo si refiere",
    "ejercicio": "nivel"
  },
  "enfermedad_actual": "narrativa cronológica del episodio actual",
  "exploracion_fisica": {
    "constantes": "TA, FC, FR, SatO2, Tª si aparecen",
    "hallazgos_relevantes": ["hallazgos exploratorios destacables"]
  },
  "pruebas_complementarias": [
    {"prueba": "nombre", "resultado": "hallazgo principal"}
  ],
  "juicio_clinico_actual": "diagnóstico o sospecha clínica registrada",
  "puntos_clave": ["3-5 puntos más importantes a recordar del caso"],
  "fecha_atencion": "fecha si aparece en la historia"
}

Si un campo no aplica o no está en la historia, usa "No especificado" o lista vacía [].
Responde SIEMPRE en español con el JSON como respuesta principal."""


@llm.tool
def search_medical_rag(query: str) -> dict:
    """Busca contexto clínico para los hallazgos descritos (CIE-10, guías, glosario).
    Args:
        query: Consulta sobre patologías o términos clínicos identificados.
    """
    pass


@retry(
    retry=retry_any(retry_if_exception_type(RateLimitError), retry_if_exception_type(ServerError)),
    wait=wait_exponential(multiplier=1, min=55, max=80),
    stop=stop_after_attempt(3),
)
@llm.call("google/gemini-2.5-flash", tools=[search_medical_rag])
def historia_agent(query: str, history: list):
    return f"""SYSTEM: {SYSTEM_PROMPT}

HISTORIAL: {history}

USER: {query}"""


async def resumir_historia(historia_clinica: str) -> str:
    history: list = []
    historia_truncada = historia_clinica[:4000]
    query_inicial = (
        f"Resume esta historia clínica extrayendo todos sus puntos clave en el formato JSON indicado. "
        f"Primero consulta la base clínica si es útil para contextualizar.\n\n"
        f"HISTORIA CLÍNICA:\n{historia_truncada}"
    )

    async with sse_client(MCP_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            current_query = query_inicial
            for _ in range(4):
                response = historia_agent(current_query, history)

                if response.tool_calls:
                    for tool_call in response.tool_calls:
                        args_dict = (
                            tool_call.args if isinstance(tool_call.args, dict)
                            else json.loads(tool_call.args)
                        )
                        print(f"[Historia] MCP: {tool_call.name}({list(args_dict.keys())})")
                        mcp_res = await session.call_tool(tool_call.name, arguments=args_dict)

                        if mcp_res.isError:
                            result_data = f"Error MCP: {mcp_res.content}"
                        else:
                            extracted = " ".join(
                                c.text for c in mcp_res.content if hasattr(c, "text")
                            )
                            try:
                                result_data = json.loads(extracted)
                            except Exception:
                                result_data = extracted

                        history.append({"role": "model", "parts": [{"text": f"Ejecuté {tool_call.name}"}]})
                        history.append({"role": "user",  "parts": [{"text": f"Resultado: {result_data}. Ahora genera el resumen JSON."}]})

                    current_query = (
                        f"Con el contexto clínico obtenido, genera el resumen completo de la historia en JSON. "
                        f"Extrae TODOS los antecedentes, medicación y hallazgos directamente del texto.\n\n"
                        f"HISTORIA CLÍNICA:\n{historia_truncada}"
                    )
                    continue

                return response.text()

    return '{"error": "El agente no pudo completar el resumen"}'


class HistoriaAgentExecutor(AgentExecutor):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        from a2a.types.a2a_pb2 import Task, TaskStatus, TASK_STATE_SUBMITTED
        user_input = context.get_user_input()

        task_obj = Task(id=context.task_id, context_id=context.context_id)
        task_obj.status.CopyFrom(TaskStatus(state=TASK_STATE_SUBMITTED))
        await event_queue.enqueue_event(task_obj)

        updater = TaskUpdater(
            event_queue=event_queue,
            task_id=context.task_id,
            context_id=context.context_id,
        )
        try:
            result = await resumir_historia(user_input)
            msg = updater.new_agent_message(parts=[Part(text=result)])
            await updater.complete(message=msg)
        except Exception as e:
            import traceback; traceback.print_exc()
            msg = updater.new_agent_message(parts=[Part(text=json.dumps({"error": str(e)}))])
            await updater.complete(message=msg)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        pass


def build_starlette_app() -> Starlette:
    agent_card = AgentCard(
        name="Resumidor de Historia Clínica MedAI",
        description="Extrae y presenta un resumen estructurado de la historia clínica del paciente: antecedentes, medicación, exploración y pruebas.",
        version="1.0.0",
        capabilities=AgentCapabilities(streaming=False),
        default_input_modes=["text"],
        default_output_modes=["text"],
        supported_interfaces=[AgentInterface(
            url=f"http://localhost:{A2A_PORT}",
            protocol_binding="HTTP+JSON",
            protocol_version="1.0",
        )],
        skills=[
            AgentSkill(
                id="resumen_historia_clinica",
                name="Resumen de Historia Clínica",
                description="Extrae datos demográficos, antecedentes, medicación, exploración y juicio clínico",
                tags=["historia clínica", "anamnesis", "SOAP", "resumen"],
                examples=["Resume esta historia clínica de urgencias"],
                input_modes=["text"],
                output_modes=["text"],
            )
        ],
    )

    task_store = InMemoryTaskStore()
    handler = DefaultRequestHandlerV2(
        agent_executor=HistoriaAgentExecutor(),
        task_store=task_store,
        agent_card=agent_card,
    )
    routes = create_agent_card_routes(agent_card) + create_rest_routes(handler)
    return Starlette(routes=routes)


if __name__ == "__main__":
    print(f"[Resumidor de Historia Clínica] Iniciando en http://localhost:{A2A_PORT}")
    uvicorn.run(build_starlette_app(), host="0.0.0.0", port=A2A_PORT)
