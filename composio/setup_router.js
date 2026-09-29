// Mints a Composio Tool Router MCP URL and writes Helios's composio MCP config.
// Reads the Composio API key from kb/.env so the secret never touches the CLI/logs.
const fs = require("fs");

function readEnv(file, key) {
  try {
    for (const line of fs.readFileSync(file, "utf8").split(/\r?\n/)) {
      const m = line.match(/^\s*([A-Za-z0-9_]+)\s*=\s*(.*)\s*$/);
      if (m && m[1] === key) return m[2].replace(/^["']|["']$/g, "").trim();
    }
  } catch {}
  return null;
}

const ENV = "C:\\Users\\Tim\\kb\\.env";
const API_KEY = process.env.COMPOSIO_API_KEY || readEnv(ENV, "COMPOSIO_API_KEY");
const USER_ID = process.env.COMPOSIO_USER_ID || readEnv(ENV, "COMPOSIO_USER_ID") || "default";
const OUT = "C:\\Users\\Tim\\helios\\config\\composio_mcp.json";

// Apps Helios should be able to reach (lazy per-app browser auth on first use).
// Only toolkits with Composio-managed auth (auto-creatable). Social apps like
// X/LinkedIn need a custom auth config in the Composio dashboard first — added later.
const TOOLKITS = [
  "gmail", "googlecalendar", "googledrive", "googledocs", "googlesheets",
  "github", "notion",
];

if (!API_KEY) { console.error("ERROR: no COMPOSIO_API_KEY in env or kb/.env"); process.exit(1); }

(async () => {
  const mod = require("@composio/core");
  const Composio = mod.Composio || mod.default || mod;
  const composio = new Composio({ apiKey: API_KEY });

  let session, err;
  for (const attempt of [
    () => composio.toolRouter.create(USER_ID, { toolkits: TOOLKITS, manageConnections: true }),
    () => composio.experimental.toolRouter.createSession(USER_ID, { toolkits: TOOLKITS, manageConnections: true }),
    () => composio.create(USER_ID, { toolkits: TOOLKITS, manageConnections: true }),
  ]) {
    try { session = await attempt(); break; } catch (e) { err = e; }
  }
  if (!session) { console.error("session create failed:", err && err.message); process.exit(2); }

  const url = session?.mcp?.url || session?.url || session?.mcpUrl || session?.mcp?.serverUrl;
  if (!url) {
    console.error("no MCP url; session keys =", JSON.stringify(Object.keys(session || {})));
    console.error(JSON.stringify(session).slice(0, 600));
    process.exit(3);
  }

  const cfg = { mcpServers: { composio: { type: "http", url, headers: { "X-API-Key": API_KEY } } } };
  fs.writeFileSync(OUT, JSON.stringify(cfg, null, 2));
  const sid = (url.match(/tool_router\/[^/]+\/([^/]+)/) || [])[1] || "";
  console.log("OK: wrote", OUT);
  console.log("host:", new URL(url).host, "| user:", USER_ID, "| session:", sid.slice(0, 8) + "…", "| toolkits:", TOOLKITS.length);
})();
