const API = "http://localhost:8080";

// ── Auth ──────────────────────────────────────────────────────────────────────

function getToken() { return localStorage.getItem("medai_token"); }

function authHeaders() {
  return {
    "Content-Type": "application/json",
    "Authorization": `Bearer ${getToken()}`,
  };
}

function logout() {
  localStorage.clear();
  window.location.href = "index.html";
}

function requireAuth() {
  if (!getToken()) window.location.href = "index.html";
}

// ── Init ──────────────────────────────────────────────────────────────────────

requireAuth();
document.getElementById("navUser").textContent =
  `👤 ${localStorage.getItem("medai_nombre") || localStorage.getItem("medai_user")}`;

// ── Agent metadata ────────────────────────────────────────────────────────────

const AGENTS = {
  medico:    { icon: "🩺", name: "Médico Diagnosticador",   color: "var(--teal)"    },
  riesgos:   { icon: "⚠️", name: "Riesgos Clínicos",         color: "var(--amber)"   },
  historia:  { icon: "📋", name: "Historia Clínica",         color: "var(--violet)"  },
  traductor: { icon: "💬", name: "Traductor Médico-Paciente", color: "var(--rose)"   },
};

const EXAMPLES = [
  "Paciente mujer de 62 años, hipertensa y diabética tipo 2 mal controlada (HbA1c 8.9), consulta por dolor torácico opresivo de 40 minutos de evolución, irradiado a brazo izquierdo y mandíbula, asociado a diaforesis profusa y náuseas. TA 90/60, FC 110, SatO2 94%. Medicación habitual: enalapril 20mg, metformina 850mg/12h, AAS 100mg, atorvastatina 40mg. Alergia a penicilina. Antecedente familiar de IAM en padre a los 58 años.",
  "Varón de 38 años sin antecedentes, consulta por cefalea de inicio súbito hace 2 horas, descrita como 'el peor dolor de cabeza de mi vida', con rigidez de nuca y fotofobia. TA 160/95, FC 88, Glasgow 14. Sin focalidad neurológica clara. No traumatismo previo. No toma medicación habitual.",
  "Niño de 6 años, fiebre de 39.5°C de 4 días de evolución, exantema maculopapular en tronco y extremidades desde ayer, conjuntivitis bilateral no purulenta, lengua aframbuesada y edema indurado en manos y pies. PCR 110, leucocitos 18.000 con neutrofilia. Sin antecedentes de interés. Vacunación completa.",
];

// ── State ─────────────────────────────────────────────────────────────────────

let isStreaming = false;
let currentEventSource = null;

// ── File drop ─────────────────────────────────────────────────────────────────

const fileInput = document.getElementById("fileInput");
fileInput.addEventListener("change", () => {
  const file = fileInput.files[0];
  if (file) document.getElementById("fileName").textContent = `📎 ${file.name}`;
});

// ── Composer ──────────────────────────────────────────────────────────────────

const composerInput = document.getElementById("composerInput");

composerInput.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    sendCase();
  }
});

function useExample(idx) {
  composerInput.value = EXAMPLES[idx];
  composerInput.focus();
}

function newConversation() {
  if (isStreaming) return;
  document.getElementById("conversation").innerHTML = `
    <div class="empty-state" id="emptyState">
      <div class="empty-icon">🩺</div>
      <h2>Nuevo análisis</h2>
      <p>Pega un caso clínico para comenzar</p>
    </div>`;
  composerInput.value = "";
  fileInput.value = "";
  document.getElementById("fileName").textContent = "";
}

// ── Conversation rendering ────────────────────────────────────────────────────

const conversation = document.getElementById("conversation");

function scrollToBottom() {
  conversation.scrollTop = conversation.scrollHeight;
}

function clearEmptyState() {
  document.getElementById("emptyState")?.remove();
}

function addUserMessage(text) {
  clearEmptyState();
  const el = document.createElement("div");
  el.className = "msg msg-user";
  el.innerHTML = `
    <div class="msg-bubble">
      <div class="msg-text">${escapeHtml(text)}</div>
    </div>
    <div class="msg-avatar avatar-user">Tú</div>
  `;
  conversation.appendChild(el);
  scrollToBottom();
}

function addRagMessage(fuentes) {
  const el = document.createElement("div");
  el.className = "msg msg-system";
  el.innerHTML = `
    <div class="system-line">
      <span class="system-icon">🗃️</span>
      <span>Contexto RAG recuperado · ${fuentes.length} fuentes consultadas (${fuentes.map(f => f.replace('.txt','')).join(', ')})</span>
    </div>
  `;
  conversation.appendChild(el);
  scrollToBottom();
}

function addAgentTyping(agentKey) {
  const a = AGENTS[agentKey];
  const el = document.createElement("div");
  el.className = "msg msg-agent";
  el.id = `agent-msg-${agentKey}`;
  el.innerHTML = `
    <div class="msg-avatar" style="background:${a.color}">${a.icon}</div>
    <div class="msg-bubble agent-bubble">
      <div class="msg-author">${a.name}</div>
      <div class="typing-indicator">
        <span></span><span></span><span></span>
      </div>
    </div>
  `;
  conversation.appendChild(el);
  scrollToBottom();
}

function updateAgentMessage(agentKey, data) {
  const el = document.getElementById(`agent-msg-${agentKey}`);
  if (!el) return;

  const a = AGENTS[agentKey];
  const bubble = el.querySelector(".msg-bubble");

  const body = renderAgentBody(agentKey, data);

  bubble.innerHTML = `
    <div class="msg-author">${a.name}</div>
    ${body}
  `;
  scrollToBottom();
}

function addErrorMessage(msg) {
  const el = document.createElement("div");
  el.className = "msg msg-system";
  el.innerHTML = `<div class="system-line error">⚠ ${escapeHtml(msg)}</div>`;
  conversation.appendChild(el);
  scrollToBottom();
}

// ── Agent body renderers ──────────────────────────────────────────────────────

function escapeHtml(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({
    '&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'
  })[c]);
}

function toArr(v) {
  if (Array.isArray(v)) return v;
  if (v && typeof v === 'object') {
    return Object.entries(v).map(([k, val]) =>
      (val && typeof val === 'object') ? val : { clave: k, valor: String(val) }
    );
  }
  if (typeof v === 'string' && v.trim()) return [v];
  return [];
}

function sevClass(s) {
  const x = (s || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");
  if (x === "critica") return "sev-critica";
  if (x === "alta")    return "sev-alta";
  if (x === "media")   return "sev-media";
  return "sev-baja";
}

function renderAgentBody(agentKey, data) {
  if (!data || data.error)  return `<div class="agent-error">⚠ ${escapeHtml(data?.error || "Sin datos")}</div>`;
  if (data.raw)             return `<pre class="agent-raw">${escapeHtml(data.raw)}</pre>`;

  if (agentKey === "medico")    return renderMedico(data);
  if (agentKey === "riesgos")   return renderRiesgos(data);
  if (agentKey === "historia")  return renderHistoria(data);
  if (agentKey === "traductor") return renderTraductor(data);
  return `<pre>${escapeHtml(JSON.stringify(data, null, 2))}</pre>`;
}

function renderMedico(d) {
  let h = "";

  if (d.recomendacion_final) {
    const r = d.recomendacion_final;
    const cls = r.includes("URGENCIAS") ? "tag-critical" : r.includes("ESPECIALISTA") ? "tag-warning" : "tag-ok";
    h += `<span class="tag ${cls}">${r.replace(/_/g," ")}</span>`;
  }
  if (d.nivel_urgencia) {
    h += `<span class="tag tag-muted">${d.nivel_urgencia}</span>`;
  }
  if (d.puntuacion_gravedad !== undefined) {
    h += `<div class="bar-line"><div class="bar-label">Gravedad ${d.puntuacion_gravedad}/100</div><div class="bar"><div class="bar-fill" style="width:${d.puntuacion_gravedad}%"></div></div></div>`;
  }

  const br = toArr(d.banderas_rojas_detectadas);
  if (br.length) {
    h += `<div class="block-title">🚨 Banderas rojas</div>`;
    br.forEach(b => {
      h += `
        <div class="block-item">
          <div class="block-head"><span class="tag ${sevClass(b.severidad)}">${b.severidad || "N/A"}</span> <strong>${escapeHtml(b.sintoma || "")}</strong></div>
          ${b.patologia_sospechada ? `<div class="block-line">→ Sospechar: ${escapeHtml(b.patologia_sospechada)}</div>` : ""}
          ${b.conducta_inmediata   ? `<div class="block-line accent">⚡ ${escapeHtml(b.conducta_inmediata)}</div>` : ""}
        </div>`;
    });
  }

  const ddx = toArr(d.diagnostico_diferencial);
  if (ddx.length) {
    h += `<div class="block-title">Diagnóstico diferencial</div>`;
    ddx.forEach(x => {
      h += `
        <div class="block-item">
          <div class="block-head"><span class="tag ${sevClass(x.probabilidad)}">${x.probabilidad || "?"}</span> <strong>${escapeHtml(x.diagnostico || "")}</strong> ${x.cie10 ? `<span class="cie10">${escapeHtml(x.cie10)}</span>` : ""}</div>
          ${x.justificacion ? `<div class="block-line">✓ ${escapeHtml(x.justificacion)}</div>` : ""}
          ${x.datos_en_contra ? `<div class="block-line muted">✗ ${escapeHtml(x.datos_en_contra)}</div>` : ""}
        </div>`;
    });
  }

  const pr = toArr(d.pruebas_recomendadas);
  if (pr.length) {
    h += `<div class="block-title">Pruebas recomendadas</div><ul class="block-list">`;
    pr.forEach(p => {
      h += `<li>${typeof p === 'string' ? escapeHtml(p) : `<strong>${escapeHtml(p.prueba || '')}</strong>${p.motivo ? ' — ' + escapeHtml(p.motivo) : ''}`}</li>`;
    });
    h += `</ul>`;
  }

  if (d.resumen_clinico) h += `<div class="block-text">${escapeHtml(d.resumen_clinico)}</div>`;
  return h;
}

function renderRiesgos(d) {
  let h = "";
  if (d.nivel_riesgo_global) h += `<span class="tag ${sevClass(d.nivel_riesgo_global)}">Nivel ${d.nivel_riesgo_global}</span>`;
  if (d.score_riesgo !== undefined) {
    h += `<div class="bar-line"><div class="bar-label">Score ${d.score_riesgo}/100</div><div class="bar"><div class="bar-fill" style="width:${d.score_riesgo}%"></div></div></div>`;
  }

  const ints = toArr(d.interacciones_farmacologicas);
  if (ints.length) {
    h += `<div class="block-title">💊 Interacciones farmacológicas</div>`;
    ints.forEach(i => {
      h += `
        <div class="block-item">
          <div class="block-head"><span class="tag ${sevClass(i.severidad)}">${i.severidad || "?"}</span> <strong>${escapeHtml(i.farmacos || "")}</strong></div>
          ${i.mecanismo ? `<div class="block-line">${escapeHtml(i.mecanismo)}</div>` : ""}
          ${i.consecuencia_clinica ? `<div class="block-line">Riesgo: ${escapeHtml(i.consecuencia_clinica)}</div>` : ""}
          ${i.recomendacion ? `<div class="block-line accent">💡 ${escapeHtml(i.recomendacion)}</div>` : ""}
        </div>`;
    });
  }

  const rs = toArr(d.riesgos_identificados);
  if (rs.length) {
    h += `<div class="block-title">Otros riesgos</div>`;
    rs.forEach(r => {
      h += `
        <div class="block-item">
          <div class="block-head"><span class="tag tag-muted">${r.tipo || "GENERAL"}</span> <span class="block-meta">Impacto ${r.impacto || "?"} · Prob ${r.probabilidad || "?"}</span></div>
          <div class="block-line">${escapeHtml(r.descripcion || "")}</div>
          ${r.mitigacion ? `<div class="block-line accent">💡 ${escapeHtml(r.mitigacion)}</div>` : ""}
        </div>`;
    });
  }

  const recs = toArr(d.recomendaciones);
  if (recs.length) {
    h += `<div class="block-title">Recomendaciones</div><ul class="block-list">`;
    recs.forEach(r => h += `<li>${typeof r === 'string' ? escapeHtml(r) : escapeHtml(JSON.stringify(r))}</li>`);
    h += `</ul>`;
  }
  return h;
}

function renderHistoria(d) {
  let h = "";
  const dp = d.datos_paciente || {};
  const demo = [];
  if (dp.edad) demo.push(`${dp.edad} años`);
  if (dp.sexo) demo.push(dp.sexo);
  if (dp.ocupacion && dp.ocupacion !== "No especificado") demo.push(dp.ocupacion);
  if (demo.length) h += `<div class="kv-row"><span class="kv-key">Paciente</span><span class="kv-val">${escapeHtml(demo.join(' · '))}</span></div>`;

  if (d.motivo_consulta && d.motivo_consulta !== "No especificado") h += `<div class="kv-row"><span class="kv-key">Motivo</span><span class="kv-val">${escapeHtml(d.motivo_consulta)}</span></div>`;
  if (d.juicio_clinico_actual && d.juicio_clinico_actual !== "No especificado") h += `<div class="kv-row"><span class="kv-key">Juicio clínico</span><span class="kv-val">${escapeHtml(d.juicio_clinico_actual)}</span></div>`;

  const ap = toArr(d.antecedentes_personales);
  if (ap.length) {
    h += `<div class="block-title">Antecedentes personales</div><ul class="block-list">`;
    ap.forEach(a => h += `<li>${typeof a === 'string' ? escapeHtml(a) : escapeHtml(JSON.stringify(a))}</li>`);
    h += `</ul>`;
  }

  const meds = toArr(d.medicacion_habitual);
  if (meds.length) {
    h += `<div class="block-title">💊 Medicación habitual</div>`;
    meds.forEach(m => {
      if (typeof m === 'string') h += `<div class="block-line">${escapeHtml(m)}</div>`;
      else h += `<div class="kv-row"><span class="kv-key">${escapeHtml(m.farmaco || '?')}</span><span class="kv-val">${escapeHtml(m.dosis || '')}${m.indicacion ? ' · ' + escapeHtml(m.indicacion) : ''}</span></div>`;
    });
  }

  const alg = toArr(d.alergias);
  if (alg.length) {
    h += `<div class="block-title">⚠️ Alergias</div><ul class="block-list">`;
    alg.forEach(a => h += `<li>${typeof a === 'string' ? escapeHtml(a) : escapeHtml(JSON.stringify(a))}</li>`);
    h += `</ul>`;
  }

  if (d.enfermedad_actual && d.enfermedad_actual !== "No especificado") {
    h += `<div class="block-title">Enfermedad actual</div><div class="block-text">${escapeHtml(d.enfermedad_actual)}</div>`;
  }

  const ef = d.exploracion_fisica || {};
  if (ef.constantes || (ef.hallazgos_relevantes && ef.hallazgos_relevantes.length)) {
    h += `<div class="block-title">Exploración</div>`;
    if (ef.constantes) h += `<div class="kv-row"><span class="kv-key">Constantes</span><span class="kv-val">${escapeHtml(ef.constantes)}</span></div>`;
    toArr(ef.hallazgos_relevantes).forEach(x => h += `<div class="block-line">${escapeHtml(typeof x === 'string' ? x : JSON.stringify(x))}</div>`);
  }

  const puntos = toArr(d.puntos_clave);
  if (puntos.length) {
    h += `<div class="block-title">Puntos clave</div><ul class="block-list">`;
    puntos.forEach(p => h += `<li>${escapeHtml(typeof p === 'string' ? p : (p.punto || JSON.stringify(p)))}</li>`);
    h += `</ul>`;
  }
  return h;
}

function renderTraductor(d) {
  let h = "";
  if (d.resumen_simple) h += `<div class="block-text big">${escapeHtml(d.resumen_simple)}</div>`;
  if (d.que_significa_tu_diagnostico) h += `<div class="block-title">¿Qué tienes?</div><div class="block-text">${escapeHtml(d.que_significa_tu_diagnostico)}</div>`;
  if (d.por_que_te_paso) h += `<div class="block-title">¿Por qué te pasó?</div><div class="block-text">${escapeHtml(d.por_que_te_paso)}</div>`;

  const pruebas = toArr(d.que_pruebas_te_haran);
  if (pruebas.length) {
    h += `<div class="block-title">Pruebas que te harán</div>`;
    pruebas.forEach(p => {
      if (typeof p === 'string') h += `<div class="block-line">${escapeHtml(p)}</div>`;
      else h += `<div class="block-item"><div class="block-head"><strong>${escapeHtml(p.prueba || '')}</strong></div>${p.para_que_sirve ? `<div class="block-line">${escapeHtml(p.para_que_sirve)}</div>` : ''}${p.como_es ? `<div class="block-line muted">${escapeHtml(p.como_es)}</div>` : ''}</div>`;
    });
  }

  const tto = toArr(d.tu_tratamiento);
  if (tto.length) {
    h += `<div class="block-title">💊 Tu tratamiento</div>`;
    tto.forEach(t => {
      if (typeof t === 'string') h += `<div class="block-line">${escapeHtml(t)}</div>`;
      else h += `<div class="block-item"><strong>${escapeHtml(t.que || '')}</strong><div class="block-line">${escapeHtml(t.como || '')}${t.por_cuanto_tiempo ? ` · ${escapeHtml(t.por_cuanto_tiempo)}` : ''}</div></div>`;
    });
  }

  if (d.señales_de_alarma?.length) {
    h += `<div class="block-title alarm">🚨 Vuelve URGENTE si tienes</div>`;
    d.señales_de_alarma.forEach(s => h += `<div class="block-line alarm">${escapeHtml(s)}</div>`);
  }

  if (d.lo_que_puedes_hacer_tu?.length) {
    h += `<div class="block-title">💡 Lo que está en tus manos</div><ul class="block-list">`;
    d.lo_que_puedes_hacer_tu.forEach(c => h += `<li>${escapeHtml(c)}</li>`);
    h += `</ul>`;
  }

  if (d.terminos_explicados?.length) {
    h += `<div class="block-title">Glosario</div>`;
    d.terminos_explicados.forEach(t => h += `<div class="kv-row"><span class="kv-key">${escapeHtml(t.termino_medico || t.termino_legal || '')}</span><span class="kv-val">${escapeHtml(t.en_simple || '')}</span></div>`);
  }

  if (d.preguntas_para_tu_medico?.length) {
    h += `<div class="block-title">Preguntas para tu médico</div><ul class="block-list">`;
    d.preguntas_para_tu_medico.forEach(p => h += `<li>${escapeHtml(p)}</li>`);
    h += `</ul>`;
  }

  if (d.mensaje_tranquilizador) h += `<div class="verdict">🩺 ${escapeHtml(d.mensaje_tranquilizador)}</div>`;
  return h;
}

// ── Send & stream ─────────────────────────────────────────────────────────────

async function sendCase() {
  if (isStreaming) return;

  const file = fileInput.files[0];
  let text = composerInput.value.trim();

  if (file && text.length < 10) {
    // Subir archivo via /analyze tradicional sería más simple, pero mantenemos el chat:
    // primero extraemos texto del PDF en el servidor a través de /analyze, luego stream.
    // Por simplicidad: si hay archivo, lo enviamos por endpoint tradicional sin streaming.
    return await sendCaseLegacyFile(file);
  }

  if (text.length < 50) {
    addErrorMessage("Pega al menos 50 caracteres describiendo el caso clínico.");
    return;
  }

  isStreaming = true;
  document.getElementById("sendBtn").disabled = true;

  addUserMessage(text);
  composerInput.value = "";

  // Inicializar las 4 burbujas en estado "esperando"
  // Las añadimos cuando llegue el evento agent_start

  try {
    const res = await fetch(`${API}/analyze/stream`, {
      method: "POST",
      headers: authHeaders(),
      body: JSON.stringify({ texto: text }),
    });

    if (!res.ok) {
      const errData = await res.json().catch(() => ({}));
      throw new Error(errData.error || `HTTP ${res.status}`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";

    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // Parsear eventos SSE
      const parts = buffer.split("\n\n");
      buffer = parts.pop();
      for (const part of parts) {
        const lines = part.split("\n");
        let eventName = "message", dataStr = "";
        for (const line of lines) {
          if (line.startsWith("event: ")) eventName = line.slice(7).trim();
          else if (line.startsWith("data: ")) dataStr += line.slice(6);
          else if (line.startsWith(":")) { /* comment */ }
        }
        if (!dataStr) continue;
        let data;
        try { data = JSON.parse(dataStr); } catch { continue; }
        handleEvent(eventName, data);
      }
    }
  } catch (e) {
    addErrorMessage(`Error: ${e.message}`);
  } finally {
    isStreaming = false;
    document.getElementById("sendBtn").disabled = false;
  }
}

async function sendCaseLegacyFile(file) {
  isStreaming = true;
  document.getElementById("sendBtn").disabled = true;
  addUserMessage(`📎 ${file.name} (PDF)`);

  ["medico","riesgos","historia","traductor"].forEach(addAgentTyping);

  try {
    const form = new FormData();
    form.append("file", file);
    const res = await fetch(`${API}/analyze`, {
      method: "POST",
      headers: { "Authorization": `Bearer ${getToken()}` },
      body: form,
    });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error || "Error del servidor");

    updateAgentMessage("medico",    data.analisis.medico_diagnosticador);
    updateAgentMessage("riesgos",   data.analisis.analista_riesgos);
    updateAgentMessage("historia",  data.analisis.historia_clinica);
    updateAgentMessage("traductor", data.analisis.traductor_paciente);
  } catch (e) {
    addErrorMessage(`Error: ${e.message}`);
  } finally {
    isStreaming = false;
    document.getElementById("sendBtn").disabled = false;
    fileInput.value = "";
    document.getElementById("fileName").textContent = "";
  }
}

function handleEvent(event, data) {
  if (event === "rag_start")  { /* opcional: mostrar "buscando contexto..." */ }
  if (event === "rag_done")   addRagMessage(data.fuentes || []);
  if (event === "agent_start") addAgentTyping(data.agent);
  if (event === "agent_done")  updateAgentMessage(data.agent, data.data);
  if (event === "complete")    { /* final */ }
  if (event === "error")       addErrorMessage(data.message || "Error desconocido");
}

// ── Búsqueda RAG (sidebar) ────────────────────────────────────────────────────

async function searchClauses() {
  const q = document.getElementById("searchQuery").value.trim();
  if (!q) return;
  const container = document.getElementById("searchResults");
  container.innerHTML = `<div class="tool-loading">Buscando...</div>`;

  try {
    const res  = await fetch(`${API}/search?q=${encodeURIComponent(q)}`, { headers: authHeaders() });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);

    container.innerHTML = data.resultados.map(r => `
      <div class="tool-result">
        <div class="tool-result-src">${escapeHtml(r.fuente)}</div>
        <div class="tool-result-text">${escapeHtml(r.contenido.slice(0, 200))}…</div>
      </div>
    `).join("") || `<div class="tool-loading">Sin resultados</div>`;
  } catch(e) {
    container.innerHTML = `<div class="tool-loading error">${e.message}</div>`;
  }
}

document.getElementById("searchQuery").addEventListener("keydown", e => { if (e.key === "Enter") searchClauses(); });

// ── Búsqueda Web MCP (sidebar) ────────────────────────────────────────────────

async function webSearch() {
  const q = document.getElementById("webQuery").value.trim();
  if (!q) return;
  const container = document.getElementById("webResults");
  container.innerHTML = `<div class="tool-loading">Consultando...</div>`;

  try {
    const res  = await fetch(`${API}/mcp-search?q=${encodeURIComponent(q)}`, { headers: authHeaders() });
    const data = await res.json();
    if (!res.ok) throw new Error(data.error);

    container.innerHTML = data.resultados.map(r => r.error ? `
      <div class="tool-result error">${escapeHtml(r.error)}</div>
    ` : `
      <div class="tool-result">
        <div class="tool-result-src">${escapeHtml(r.titulo || "")}</div>
        <div class="tool-result-text">${escapeHtml((r.texto || "").slice(0, 180))}…</div>
        ${r.url ? `<a class="tool-result-link" href="${escapeHtml(r.url)}" target="_blank">Abrir →</a>` : ""}
      </div>
    `).join("") || `<div class="tool-loading">Sin resultados</div>`;
  } catch(e) {
    container.innerHTML = `<div class="tool-loading error">${e.message}</div>`;
  }
}

document.getElementById("webQuery").addEventListener("keydown", e => { if (e.key === "Enter") webSearch(); });
