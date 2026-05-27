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
A2A_PORT = 8004

SYSTEM_PROMPT = """Eres el Traductor Médico-Paciente de MedAI. Tu especialidad es explicar la
información clínica (diagnósticos, pruebas, tratamientos) en términos simples y empáticos que
cualquier persona sin formación médica pueda entender.

⚠️ DISCLAIMER: Esta explicación tiene fines educativos y de apoyo. Cualquier decisión sobre
tu salud debe consultarse con tu médico tratante.

TU MISIÓN: Convertir la jerga médica en lenguaje cotidiano, empático y claro, evitando alarmar
innecesariamente pero comunicando con honestidad lo que el paciente necesita saber.

PROCESO OBLIGATORIO:
1. Llama a explain_medical_term para obtener explicaciones simples de los términos técnicos más importantes.
2. Analiza el caso clínico identificando lo que un paciente necesita entender.
3. Entrega la traducción en este JSON EXACTO:

{
  "agente": "Traductor Médico-Paciente",
  "resumen_simple": "Explicación en 3-4 oraciones simples de lo que le está pasando al paciente",
  "que_significa_tu_diagnostico": "Explicación cotidiana del diagnóstico o sospecha clínica",
  "por_que_te_paso": "Posibles causas explicadas en lenguaje claro",
  "que_pruebas_te_haran": [
    {"prueba": "nombre simple", "para_que_sirve": "qué buscan con ella", "como_es": "cómo se hace"}
  ],
  "tu_tratamiento": [
    {"que": "qué tienes que hacer/tomar", "como": "cómo y cuándo", "por_cuanto_tiempo": "duración"}
  ],
  "señales_de_alarma": ["síntomas por los que debes volver a consultar urgente"],
  "lo_que_puedes_hacer_tu": ["cuidados, hábitos o cambios que están en tus manos"],
  "terminos_explicados": [
    {
      "termino_medico": "término técnico que aparece en tu informe",
      "en_simple": "qué significa en palabras normales"
    }
  ],
  "preguntas_para_tu_medico": ["preguntas útiles para tu próxima consulta"],
  "mensaje_tranquilizador": "una frase honesta y empática sobre tu situación"
}

Usa lenguaje de todos los días. Evita tecnicismos. Si debes usarlos, explícalos inmediatamente.
NUNCA prometas curaciones ni minimices síntomas graves.
Responde SIEMPRE en español con el JSON como respuesta principal."""


@llm.tool
def explain_medical_term(term: str) -> dict:
    """Explica un término médico en lenguaje sencillo consultando el glosario clínico.
    Args:
        term: Término médico a explicar (ej: 'disnea', 'taquicardia', 'comorbilidad').
    """
    pass


@retry(
    retry=retry_any(retry_if_exception_type(RateLimitError), retry_if_exception_type(ServerError)),
    wait=wait_exponential(multiplier=1, min=55, max=80),
    stop=stop_after_attempt(3),
)
@llm.call("google/gemini-2.5-flash", tools=[explain_medical_term])
def traductor_agent(query: str, history: list):
    return f"""SYSTEM: {SYSTEM_PROMPT}

HISTORIAL: {history}

USER: {query}"""


async def traducir_para_paciente(caso_clinico: str) -> str:
    history: list = []
    caso_truncado = caso_clinico[:4000]
    query_inicial = (
        f"Traduce esta información clínica al lenguaje cotidiano que un paciente entiende. "
        f"Identifica los términos técnicos clave, consúltalos si es necesario, y genera la traducción completa en JSON.\n\n"
        f"CASO CLÍNICO:\n{caso_truncado}"
    )

    async with sse_client(MCP_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            current_query = query_inicial
            for _ in range(5):
                response = traductor_agent(current_query, history)

                if response.tool_calls:
                    for tool_call in response.tool_calls:
                        args_dict = (
                            tool_call.args if isinstance(tool_call.args, dict)
                            else json.loads(tool_call.args)
                        )
                        print(f"[Traductor] MCP: {tool_call.name}({list(args_dict.keys())})")
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
                        history.append({"role": "user",  "parts": [{"text": f"Resultado: {result_data}. Continúa con la traducción."}]})

                    current_query = (
                        f"Con las explicaciones obtenidas, genera la traducción completa para el paciente en JSON. "
                        f"Explica cada parte en lenguaje cotidiano.\n\nCASO CLÍNICO:\n{caso_truncado}"
                    )
                    continue

                return response.text()

    return '{"error": "El agente no pudo completar la traducción"}'


class TraductorAgentExecutor(AgentExecutor):
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
            result = await traducir_para_paciente(user_input)
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
        name="Traductor Médico-Paciente MedAI",
        description="Explica diagnósticos, pruebas y tratamientos en lenguaje cotidiano y empático para el paciente.",
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
                id="traduccion_medica",
                name="Traducción Médica a Lenguaje del Paciente",
                description="Convierte jerga médica en lenguaje cotidiano accesible para pacientes y familiares",
                tags=["comunicación", "salud", "lenguaje simple", "educación sanitaria"],
                examples=["Explícame qué significa este informe médico en palabras simples"],
                input_modes=["text"],
                output_modes=["text"],
            )
        ],
    )

    task_store = InMemoryTaskStore()
    handler = DefaultRequestHandlerV2(
        agent_executor=TraductorAgentExecutor(),
        task_store=task_store,
        agent_card=agent_card,
    )
    routes = create_agent_card_routes(agent_card) + create_rest_routes(handler)
    return Starlette(routes=routes)


if __name__ == "__main__":
    print(f"[Traductor Médico-Paciente] Iniciando en http://localhost:{A2A_PORT}")
    uvicorn.run(build_starlette_app(), host="0.0.0.0", port=A2A_PORT)
