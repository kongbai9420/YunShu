import ELK, { type ELK as ElkInstance } from "elkjs/lib/elk-api.js"
// elkjs 自带的 Web Worker 版本：布局计算在后台线程里跑，不卡页面；文件原样复制到构建产物（EPL-2.0，未修改）
import elkWorkerUrl from "elkjs/lib/elk-worker.min.js?url"

export const NODE_WIDTH = 184
export const NODE_HEIGHT = 68

export interface LayoutInput {
  id: string
  locked: boolean
}

let elk: ElkInstance | null = null

function instance(): ElkInstance {
  elk ??= new ELK({ workerUrl: elkWorkerUrl })
  return elk
}

/**
 * 用 ELK layered 算法计算位置，返回未锁定节点的新坐标；锁定节点保持原位，不在结果里。
 * Worker 只在第一次布局时创建，之后复用。
 */
export async function elkLayout(
  nodes: LayoutInput[],
  edges: { id: string; source: string; target: string }[],
): Promise<Map<string, { x: number; y: number }>> {
  const ids = new Set(nodes.map((n) => n.id))
  const graph = await instance().layout({
    id: "root",
    layoutOptions: {
      "elk.algorithm": "layered",
      "elk.direction": "RIGHT",
      "elk.layered.spacing.nodeNodeBetweenLayers": "96",
      "elk.spacing.nodeNode": "28",
      "elk.spacing.componentComponent": "48",
      "elk.layered.nodePlacement.strategy": "BRANDES_KOEPF",
    },
    children: nodes.map((n) => ({ id: n.id, width: NODE_WIDTH, height: NODE_HEIGHT })),
    edges: edges
      .filter((e) => ids.has(e.source) && ids.has(e.target))
      .map((e) => ({ id: e.id, sources: [e.source], targets: [e.target] })),
  })
  const locked = new Set(nodes.filter((n) => n.locked).map((n) => n.id))
  const out = new Map<string, { x: number; y: number }>()
  for (const c of graph.children ?? []) if (!locked.has(c.id)) out.set(c.id, { x: c.x ?? 0, y: c.y ?? 0 })
  return out
}
