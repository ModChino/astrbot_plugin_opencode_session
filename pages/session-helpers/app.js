// Helper page for the "contextless" session value.
//
// The WebUI schema form has no button widget, so generating a UUID needs this
// small custom page. Everything goes through the plugin page bridge; the iframe
// cannot reach the Dashboard session directly.

const bridge = window.AstrBotPluginPage;

const input = document.getElementById("value");
const status = document.getElementById("status");
const note = document.getElementById("note");

function setStatus(text, kind = "") {
  status.textContent = text;
  status.className = kind;
}

function randomUuid() {
  if (window.crypto && typeof window.crypto.randomUUID === "function") {
    return window.crypto.randomUUID();
  }
  // Fallback for older embedded browsers: RFC 4122 version 4 layout.
  const bytes = new Uint8Array(16);
  window.crypto.getRandomValues(bytes);
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
  return [
    hex.slice(0, 8),
    hex.slice(8, 12),
    hex.slice(12, 16),
    hex.slice(16, 20),
    hex.slice(20),
  ].join("-");
}

async function load() {
  try {
    const data = await bridge.apiGet("session-helpers");
    input.value = data.contextless_session_id ?? "";
    note.textContent =
      `当前生效的请求头：${data.target_header}　·　匹配方式：${data.match_mode}` +
      "　·　想每次随机，请在插件配置里打开「每次测试请求随机生成 UUID」。";
  } catch (error) {
    setStatus(`读取失败：${error.message}`, "err");
  }
}

document.getElementById("generate").addEventListener("click", () => {
  input.value = randomUuid();
  setStatus("已生成，点「保存」写入配置");
});

document.getElementById("save").addEventListener("click", async () => {
  const value = input.value.trim();
  if (!value) {
    setStatus("不能为空：上游会因此返回 400", "err");
    return;
  }
  try {
    const result = await bridge.apiPost("session-helpers/save", {
      contextless_session_id: value,
    });
    setStatus(result.saved ? "已保存" : "已生效（写入配置文件失败）", result.saved ? "ok" : "err");
  } catch (error) {
    setStatus(`保存失败：${error.message}`, "err");
  }
});

await bridge.ready();
await load();
