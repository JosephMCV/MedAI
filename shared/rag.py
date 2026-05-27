import os
from dotenv import load_dotenv

load_dotenv(dotenv_path=os.path.join(os.path.dirname(__file__), "../.env"))

from langchain_google_genai import GoogleGenerativeAIEmbeddings
from langchain_chroma import Chroma
from langchain_text_splitters import RecursiveCharacterTextSplitter
from langchain_core.documents import Document

KNOWLEDGE_DIR = os.path.join(os.path.dirname(__file__), "knowledge")
CHROMA_PATH   = os.path.join(os.path.dirname(__file__), "chroma_db")

_vectorstore = None


def _load_knowledge_docs() -> list[Document]:
    docs = []
    for fname in os.listdir(KNOWLEDGE_DIR):
        if not fname.endswith(".txt"):
            continue
        fpath = os.path.join(KNOWLEDGE_DIR, fname)
        with open(fpath, "r", encoding="utf-8") as f:
            content = f.read()
        docs.append(Document(page_content=content, metadata={"source": fname}))
    return docs


def init_rag():
    global _vectorstore
    if _vectorstore is not None:
        return _vectorstore

    embeddings = GoogleGenerativeAIEmbeddings(
        model="models/gemini-embedding-001",
        google_api_key=os.getenv("GOOGLE_API_KEY"),
    )

    # Cargar DB existente
    if os.path.exists(CHROMA_PATH) and os.listdir(CHROMA_PATH):
        print("[RAG] Cargando base vectorial existente...")
        _vectorstore = Chroma(
            embedding_function=embeddings,
            persist_directory=CHROMA_PATH,
        )
        return _vectorstore

    # Crear nueva DB desde archivos de conocimiento
    print("[RAG] Creando base vectorial desde knowledge base...")
    raw_docs = _load_knowledge_docs()

    splitter = RecursiveCharacterTextSplitter(chunk_size=600, chunk_overlap=80)
    chunks = splitter.split_documents(raw_docs)

    _vectorstore = Chroma.from_documents(
        documents=chunks,
        embedding=embeddings,
        persist_directory=CHROMA_PATH,
    )
    print(f"[RAG] Base vectorial creada con {len(chunks)} fragmentos.")
    return _vectorstore


def search_rag(query: str, k: int = 4) -> list[dict]:
    vs = init_rag()
    results = vs.similarity_search_with_score(query, k=k)
    return [
        {
            "contenido": doc.page_content,
            "fuente": doc.metadata.get("source", "desconocido"),
            "relevancia": round(float(score), 4),
        }
        for doc, score in results
    ]
