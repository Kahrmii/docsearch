// docsearch web interface. All text coming from documents or the model is inserted with textContent / text nodes,
// never as HTML.
(() => {
  "use strict";
  const token = document.querySelector('meta[name="token"]').content;
  const $ = (id) => document.getElementById(id);
  const chatId = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : String(Math.random()).slice(2);
  let state = { indices: [], providers: [], settings: {} };
  let chatPoll = null, chatSince = 0, indexPoll = null, indexSince = 0, firstLoad = true;

  async function api(path, body) {
    const options = { headers: { "X-Docsearch-Token": token } };
    if (body !== undefined) {
      options.method = "POST";
      options.headers["Content-Type"] = "application/json";
      options.body = JSON.stringify(body);
    }
    let res;
    try {
      res = await fetch(path, options);
    } catch (e) {
      return { ok: false, status: 0, data: { error: "The server is not reachable." } };
    }
    let data = {};
    try { data = await res.json(); } catch (e) { /* no JSON body */ }
    return { ok: res.ok, status: res.status, data };
  }

  function line(kind, text) {
    const div = document.createElement("div");
    div.className = kind;
    div.textContent = text;
    $("transcript").append(div);
    $("transcript").scrollTop = $("transcript").scrollHeight;
    return div;
  }

  // ---- tabs
  function showTab(name) {
    for (const tab of ["ask", "index"]) {
      $("panel-" + tab).hidden = tab !== name;
      $("tab-" + tab).classList.toggle("active", tab === name);
    }
  }
  $("tab-ask").addEventListener("click", () => showTab("ask"));
  $("tab-index").addEventListener("click", () => showTab("index"));

  // ---- state (indices, models, Docker status)
  function fillSelect(select, options, selected) {
    select.replaceChildren(...options.map(([value, label]) => {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = label;
      return option;
    }));
    if (selected && options.some(([value]) => value === selected)) select.value = selected;
  }

  function syncCloudBox() {
    const current = state.indices.find((i) => i.name === $("ask-index").value);
    $("ask-cloud").checked = Boolean(current && current.cloud_allowed);
  }

  async function loadState() {
    const { ok, data } = await api("/api/state");
    if (!ok) return;
    const names = data.indices.map((i) => i.name).join(",");
    const changed = names !== state.indices.map((i) => i.name).join(",");
    state = data;
    $("docker").textContent = data.docker + (data.es_error ? "  |  " + data.es_error : "");
    if (firstLoad || changed) {
      const keepIndex = firstLoad ? data.settings.chat_index : $("ask-index").value;
      const labelled = data.indices.map((i) => [i.name, `${i.name} (${i.documents})`]);
      fillSelect($("ask-index"), labelled, keepIndex);
      fillSelect($("delete-index"), labelled, $("delete-index").value);
      if (firstLoad) {
        fillSelect($("ask-model"), data.providers.map((p) => [p.key, p.label]), data.settings.chat_provider);
        $("index-folder").value = data.settings.folder || "";
        $("index-name").value = data.settings.index || "documents";
        $("index-recursive").checked = data.settings.recursive !== false;
        firstLoad = false;
      }
    }
    syncCloudBox();
  }
  $("ask-index").addEventListener("change", syncCloudBox);

  $("ask-cloud").addEventListener("change", async () => {
    const index = $("ask-index").value, allowed = $("ask-cloud").checked;
    let res = await api("/api/cloud", { index, allowed });
    if (res.data.needs_confirmation) {
      if (!window.confirm(res.data.message)) { $("ask-cloud").checked = false; return; }
      res = await api("/api/cloud", { index, allowed, confirm: true });
    }
    if (!res.ok) { $("ask-cloud").checked = !allowed; line("error", res.data.error || "The change failed."); }
    await loadState();
  });

  // ---- ask
  function linkify(container, text, known) {
    const bracket = /\[([^\[\]]+)\]/g;
    let last = 0, match;
    while ((match = bracket.exec(text)) !== null) {
      container.append(document.createTextNode(text.slice(last, match.index) + "["));
      for (const part of match[1].split(/([,;])/)) {
        const name = part.trim();
        if (!known.has(name)) { container.append(document.createTextNode(part)); continue; }
        const start = part.indexOf(name);
        const link = document.createElement("a");
        link.href = "#";
        link.textContent = name;
        link.addEventListener("click", (event) => { event.preventDefault(); openSource(name); });
        container.append(document.createTextNode(part.slice(0, start)), link,
                         document.createTextNode(part.slice(start + name.length)));
      }
      container.append(document.createTextNode("]"));
      last = match.index + match[0].length;
    }
    container.append(document.createTextNode(text.slice(last)));
  }

  // **bold** becomes <strong> (built with the DOM, never from HTML); everything else stays plain text
  function richText(container, text, known) {
    text.split(/(\*\*[^*\n]+\*\*)/).forEach((part) => {
      if (part.length > 4 && part.startsWith("**") && part.endsWith("**")) {
        const strong = document.createElement("strong");
        linkify(strong, part.slice(2, -2), known);
        container.append(strong);
      } else {
        linkify(container, part, known);
      }
    });
  }

  async function openSource(filename) {
    const res = await api("/api/open", { chat_id: chatId, filename });
    if (!res.ok) line("error", res.data.error || "Could not open the file.");
  }

  function setBusy(busy) {
    $("ask-send").disabled = busy;
    $("ask-stop").disabled = !busy;
    $("ask-input").disabled = busy;
    if (!busy) $("ask-input").focus();
  }

  function render(event) {
    if (event.type === "user") line("user", "You: " + event.text);
    else if (event.type === "step") {
      const shown = Object.entries(event.args).map(([k, v]) => `${k}=${JSON.stringify(v)}`).join(", ");
      line("step", `  -> ${event.name}(${shown.slice(0, 160)})`);
      $("ask-status").textContent = `Tool: ${event.name} ...`;
    } else if (event.type === "status") $("ask-status").textContent = event.text;
    else if (event.type === "answer") richText(line("answer", ""), event.text, new Set(event.sources));
    else if (event.type === "error") line("error", event.text);
    else if (event.type === "info") line("info", event.text);
    else if (event.type === "done") $("ask-status").textContent = "Ready.";
  }

  async function pollChat() {
    const res = await api(`/api/chat/poll?chat_id=${encodeURIComponent(chatId)}&since=${chatSince}`);
    if (!res.ok) return;
    chatSince = res.data.next;
    res.data.events.forEach(render);
    if (res.data.events.some((e) => e.type === "done")) {
      clearInterval(chatPoll);
      chatPoll = null;
      setBusy(false);
    }
  }

  $("ask-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const question = $("ask-input").value.trim(), index = $("ask-index").value, provider = $("ask-model").value;
    if (!question || !index || chatPoll) return;
    const body = { chat_id: chatId, question, index, provider };
    let res = await api("/api/chat/ask", body);
    if (res.data.needs_confirmation) {
      if (!window.confirm(res.data.message)) {
        line("error", "Cancelled: nothing was sent to the cloud. Choose the local model for this index.");
        return;
      }
      res = await api("/api/chat/ask", { ...body, confirm: true });
    }
    if (!res.ok) { line("error", res.data.error || `Error ${res.status}`); return; }
    $("ask-input").value = "";
    setBusy(true);
    $("ask-status").textContent = "Working ...";
    chatPoll = setInterval(pollChat, 400);
    pollChat();
  });

  $("ask-stop").addEventListener("click", () => api("/api/chat/stop", { chat_id: chatId }));
  $("ask-new").addEventListener("click", async () => {
    const res = await api("/api/chat/reset", { chat_id: chatId });
    if (!res.ok) { line("error", res.data.error || "Could not start a new conversation."); return; }
    $("transcript").replaceChildren();
    chatSince = 0;
    $("ask-status").textContent = "New conversation.";
  });

  // ---- index
  function setIndexBusy(running) {
    $("index-start").disabled = running;
    $("index-count").disabled = running;
    $("index-delete").disabled = running;
    $("index-stop").disabled = !running;
  }

  // Deleting is permanent: the server gives the warning text, the user has to type the index name, and the
  // server checks the typed name again.
  $("index-delete").addEventListener("click", async () => {
    const index = $("delete-index").value;
    if (!index) return;
    const info = await api(`/api/index/info?index=${encodeURIComponent(index)}`);
    if (!info.ok) { $("index-status").textContent = info.data.error || `Error ${info.status}`; return; }
    const typed = window.prompt(info.data.warning + "\n\nType the index name to confirm:");
    if (typed === null) return;  // cancelled
    const res = await api("/api/index/delete", { index, confirm: typed.trim() });
    $("index-status").textContent = res.ok ? `Index '${index}' deleted (${res.data.documents} documents).`
                                           : (res.data.error || `Error ${res.status}`);
    await loadState();
  });

  async function pollIndex() {
    const res = await api(`/api/index/status?since=${indexSince}`);
    if (!res.ok) return;
    const s = res.data;
    indexSince = s.next;
    if (s.log.length) {
      const log = $("index-log");
      log.append(document.createTextNode(s.log.join("\n") + "\n"));
      log.scrollTop = log.scrollHeight;
    }
    if (s.progress) {
      const p = s.progress, pct = 100 * p.done / Math.max(p.total, 1);
      $("index-progress").value = pct;
      const eta = p.eta_s ? `${Math.floor(p.eta_s / 3600)}h${String(Math.floor(p.eta_s % 3600 / 60)).padStart(2, "0")}m` : "?";
      $("index-status").textContent = `File ${p.done}/${p.total} (${pct.toFixed(1)}%) | ok ${p.ok} | errors ${p.errors} | ` +
        `skipped ${p.skipped} | ${p.rate.toFixed(2)}/s | left ${eta}`;
    } else if (s.status) $("index-status").textContent = s.status;
    if (!s.running) {
      clearInterval(indexPoll);
      indexPoll = null;
      setIndexBusy(false);
      if (s.message) { $("index-status").textContent = s.message; }
      await loadState();
    }
  }

  async function startIndex(dryRun) {
    const body = { folder: $("index-folder").value.trim(), index: $("index-name").value.trim().toLowerCase(),
                   recursive: $("index-recursive").checked, dry_run: dryRun };
    const res = await api("/api/index/start", body);
    if (!res.ok) { $("index-status").textContent = res.data.error || `Error ${res.status}`; return; }
    $("index-log").replaceChildren();
    $("index-progress").value = 0;
    indexSince = 0;
    setIndexBusy(true);
    indexPoll = setInterval(pollIndex, 1000);
    pollIndex();
  }
  $("index-start").addEventListener("click", () => startIndex(false));
  $("index-count").addEventListener("click", () => startIndex(true));
  $("index-stop").addEventListener("click", () => api("/api/index/stop", {}));
  $("index-browse").addEventListener("click", async () => {
    $("index-status").textContent = "Choose a folder in the dialog ...";
    const res = await api("/api/pick-folder");
    $("index-status").textContent = res.data.error || "Ready.";
    if (res.data.path) $("index-folder").value = res.data.path;
  });

  loadState();
  setInterval(loadState, 5000);
})();
