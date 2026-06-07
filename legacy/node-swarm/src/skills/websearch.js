// Web Search skill using Exa API
import { fileURLToPath } from "url";
import path from "path";
import fs from "fs";
import YAML from "yaml";

const __dirname = path.dirname(fileURLToPath(import.meta.url));

// Load API key from config
function loadConfig() {
  try {
    // Try multiple paths
    const paths = [
      path.join(__dirname, "../../../config/user.yaml"),
      path.join(__dirname, "../../config/user.yaml"),
      path.join(process.cwd(), "../config/user.yaml"),
      path.join(process.cwd(), "config/user.yaml"),
    ];

    for (const configPath of paths) {
      if (fs.existsSync(configPath)) {
        console.error(`Found config at: ${configPath}`);
        const configFile = fs.readFileSync(configPath, "utf8");
        return YAML.parse(configFile);
      }
    }
    console.error("Config paths tried:", paths);
    return { api_keys: {} };
  } catch (e) {
    console.error("Config load error:", e.message);
    return { api_keys: {} };
  }
}

const EXA_API_BASE = "https://api.exa.ai";

/**
 * Search the web using Exa API
 * @param {string} query - Search query
 * @param {number} numResults - Number of results to return (default: 10)
 * @returns {Promise<Array>} Search results
 */
export async function search(query, numResults = 10) {
  const config = loadConfig();
  const apiKey = config.api_keys?.exa;

  if (!apiKey) {
    throw new Error(
      "Exa API key not configured. Add to config/user.yaml: api_keys.exa",
    );
  }

  const response = await fetch(`${EXA_API_BASE}/search`, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      "x-api-key": apiKey,
    },
    body: JSON.stringify({
      query: query,
      numResults: numResults,
      type: "keyword",
    }),
  });

  if (!response.ok) {
    const error = await response.text();
    throw new Error(`Exa API error: ${response.status} - ${error}`);
  }

  const data = await response.json();
  return data.results || [];
}

/**
 * Find and return formatted search results
 * @param {string} query - Search query
 * @returns {Promise<string>} Formatted results
 */
export async function find(query) {
  const results = await search(query, 5);

  if (results.length === 0) {
    return `No results found for: ${query}`;
  }

  const formatted = results
    .map((r, i) => {
      return `${i + 1}. ${r.title}\n   ${r.url}\n   ${r.snippet?.substring(0, 150)}...`;
    })
    .join("\n\n");

  return `Results for "${query}":\n\n${formatted}`;
}

// CLI test
if (import.meta.url === `file://${process.argv[1]}`) {
  const query = process.argv.slice(2).join(" ") || "test";
  console.log(`Searching for: ${query}...`);
  find(query).then(console.log).catch(console.error);
}
