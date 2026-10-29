import { defineConfig } from "@playwright/test"
import { existsSync } from "node:fs"

const python =
  process.env.YUNSHU_TEST_PYTHON ?? (process.platform === "win32" ? "../backend/.venv/Scripts/python.exe" : "python")
const installedBrowser =
  process.platform === "win32" && existsSync("C:/Program Files/Google/Chrome/Application/chrome.exe")
    ? "chrome"
    : undefined

export default defineConfig({
  testDir: "./e2e",
  fullyParallel: false,
  workers: 1,
  timeout: 90000,
  expect: { timeout: 15000 },
  reporter: [["list"], ["json", { outputFile: "test-results/summary.json" }]],
  use: {
    baseURL: "http://127.0.0.1:5185",
    channel: process.env.PLAYWRIGHT_CHANNEL ?? installedBrowser,
    trace: "retain-on-failure",
    screenshot: "only-on-failure",
  },
  webServer: [
    {
      command: `"${python}" e2e/server.py`,
      url: "http://127.0.0.1:8015/api/health",
      timeout: 60000,
      reuseExistingServer: false,
    },
    {
      command: "npm run dev -- --host 127.0.0.1 --port 5185 --strictPort",
      url: "http://127.0.0.1:5185",
      env: { YUNSHU_API_URL: "http://127.0.0.1:8015" },
      timeout: 60000,
      reuseExistingServer: false,
    },
  ],
})
