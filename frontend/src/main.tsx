import { StrictMode } from "react"
import { createRoot } from "react-dom/client"

import "./index.css"
import "./themes/glass.css"
import App from "./App.tsx"

const rootElement = document.getElementById("root")
if (!rootElement) {
  throw new Error("找不到 #root 挂载点")
}

createRoot(rootElement).render(
  <StrictMode>
    <App />
  </StrictMode>,
)
