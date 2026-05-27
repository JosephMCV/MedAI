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
A2A_PORT = 8002

SYSTEM_PROMPT = """Eres el Analista de Riesgos Clínicos de MedAI, experto en farmacovigilancia,
comorbilidades, interacciones medicamentosas y estratificación de riesgo cardiovascular y oncológico.

⚠️ DISCLAIMER: Esta evaluación es de APOYO. Las decisiones farmacológicas y de seguimiento
deben ser validadas por un médico responsable del paciente.

TU MISIÓN: Identificar y cuantificar los riesgos clínicos del paciente — interacciones
farmacológicas, contraindicaciones, comorbilidades agravantes, riesgo cardiovascular,
riesgo oncológico, riesgo de yatrogenia.

PROCESO OBLIGATORIO:
1. Llama a search_medical_rag para buscar interacciones, contraindicaciones y guías relevantes.
2. Opcionalmente usa search_web para alertas farmacológicas o información actualizada.
3. Analiza el caso clínico evaluando: riesgo farmacológico, comorbilidades, riesgo CV, riesgo oncológico, riesgo de complicaciones.
4. Entrega el análisis en este JSON EXACTO:

{
  "agente": "Analista de Riesgos Clínicos",
  "nivel_riesgo_global": "CRÍTICO|ALTO|MEDIO|BAJO",
  "score_riesgo": 0,
  "riesgos_identificados": [
    {
      "tipo": "FARMACOLOGICO|CARDIOVASCULAR|ONCOLOGICO|INFECCIOSO|METABOLICO|YATROGENICO",
      "descripcion": "descripción del riesgo",
      "probabilidad": "ALTA|MEDIA|BAJA",
      "impacto": "ALTO|MEDIO|BAJO",
      "mitigacion": "cómo prevenir o monitorizar este riesgo"
    }
  ],
  "interacciones_farmacologicas": [
    {
      "farmacos": "fármacos implicados",
      "mecanismo": "mecanismo de la interacción",
      "consecuencia_clinica": "efecto adverso esperado",
      "severidad": "CRÍTICA|ALTA|MEDIA|BAJA",
      "recomendacion": "ajuste, suspensión o monitorización"
    }
  ],
  "factores_protectores": ["factores que reducen el riesgo del paciente"],
  "recomendaciones": ["recomendaciones de manejo concretas"],
  "conclusion": "síntesis del perfil de riesgo del paciente"
}

El score_riesgo es de 0 (sin riesgo) a 100 (riesgo máximo de evento adverso).
Responde SIEMPRE en español con el JSON como respuesta principal."""


@llm.tool
def search_medical_rag(query: str) -> dict:
    """Busca en la base de conocimiento clínica (interacciones, guías, contraindicaciones).
    Args:
        query: Consulta sobre fármacos, interacciones o riesgos clínicos.
    """
    pass

@llm.tool
def search_web(query: str) -> dict:
    """Busca información médica actualizada en fuentes confiables (NIH, OMS, NICE).
    Args:
        query: Búsqueda sobre alertas farmacológicas o riesgos clínicos.
    """
    pass


@retry(
    retry=retry_any(retry_if_exception_type(RateLimitError), retry_if_exception_type(ServerError)),
    wait=wait_exponential(multiplier=1, min=55, max=80),
    stop=stop_after_attempt(3),
)
@llm.call("google/gemini-2.5-flash", tools=[search_medical_rag, search_web])
def riesgos_agent(query: str, history: list):
    return f"""SYSTEM: {SYSTEM_PROMPT}

HISTORIAL: {history}

USER: {query}"""


async def analizar_riesgos(caso_clinico: str) -> str:
    history: list = []
    caso_truncado = caso_clinico[:4000]
    query_inicial = (
        f"Como analista de riesgos clínicos, evalúa los riesgos farmacológicos, cardiovasculares "
        f"y de complicaciones de este paciente. Consulta la base clínica y emite tu análisis en JSON.\n\n"
        f"CASO CLÍNICO:\n{caso_truncado}"
    )

    async with sse_client(MCP_URL) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            current_query = query_inicial
            for _ in range(5):
                response = riesgos_agent(current_query, history)

                if response.tool_calls:
                    for tool_call in response.tool_calls:
                        args_dict = (
                            tool_call.args if isinstance(tool_call.args, dict)
                            else json.loads(tool_call.args)
                        )
                        print(f"[Riesgos] MCP: {tool_call.name}({list(args_dict.keys())})")
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
                        history.append({"role": "user",  "parts": [{"text": f"Resultado: {result_data}. Continúa."}]})

                    current_query = (
                        f"Con la información obtenida, entrega el análisis de riesgos clínicos completo en JSON. "
                        f"Evalúa cada riesgo identificado para este paciente.\n\nCASO CLÍNICO:\n{caso_truncado}"
                    )
                    continue

                return response.text()

    return '{"error": "El agente no pudo completar el análisis"}'


class RiesgosAgentExecutor(AgentExecutor):
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
            result = await analizar_riesgos(user_input)
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
        name="Analista de Riesgos Clínicos MedAI",
        description="Evalúa riesgos farmacológicos, cardiovasculares, oncológicos y de yatrogenia en pacientes.",
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
                id="analisis_riesgos_clinicos",
                name="Análisis de Riesgos Clínicos",
                description="Detecta interacciones farmacológicas, comorbilidades y riesgo de complicaciones",
                tags=["farmacovigilancia", "riesgo cardiovascular", "interacciones", "comorbilidades"],
                examples=["Paciente con warfarina + nuevo AINE prescrito"],
                input_modes=["text"],
                output_modes=["text"],
            )
        ],
    )

    task_store = InMemoryTaskStore()
    handler = DefaultRequestHandlerV2(
        agent_executor=RiesgosAgentExecutor(),
        task_store=task_store,
        agent_card=agent_card,
    )
    routes = create_agent_card_routes(agent_card) + create_rest_routes(handler)
    return Starlette(routes=routes)


if __name__ == "__main__":
    print(f"[Analista de Riesgos Clínicos] Iniciando en http://localhost:{A2A_PORT}")
    uvicorn.run(build_starlette_app(), host="0.0.0.0", port=A2A_PORT)
