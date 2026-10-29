import { Handle, Position, type Node, type NodeProps } from "@xyflow/react"
import { History, Lock } from "lucide-react"

import type { TopoNode } from "@/lib/topology"
import { cn } from "@/lib/utils"
import { FORM_LABEL, ROLE_LABEL, labelOf } from "@/lib/vocab"
import { NODE_HEIGHT, NODE_WIDTH } from "./elkLayout"

export type AssetNodeData = {
  asset: TopoNode
  locked: boolean
  /** 机房视图显示机柜与 U 位，家用视图显示平台 */
  datacenter: boolean
  /** 刚被撤销 / 重做影响到的节点：画布上高亮并写明原因（不只靠颜色） */
  flash: "" | "undo" | "redo"
  connectable: boolean
}
export type AssetFlowNode = Node<AssetNodeData, "asset">

function subtitle(asset: TopoNode, datacenter: boolean): string {
  if (datacenter) {
    const rack = asset.rack_name ? `机柜 ${asset.rack_name}${asset.rack_unit != null ? ` · U${asset.rack_unit}` : ""}` : "未放入机柜"
    return rack
  }
  return asset.platform || labelOf(FORM_LABEL, asset.form)
}

export function AssetNode({ data, selected }: NodeProps<AssetFlowNode>) {
  const { asset, locked, flash, datacenter, connectable } = data
  return (
    <div
      className={cn(
        "flex flex-col justify-center gap-1 rounded-lg border bg-card px-3 py-2 text-left shadow-card",
        selected ? "border-ring ring-2 ring-ring/40" : "border-input",
        flash && "outline-2 outline-offset-2 outline-dashed outline-warn",
      )}
      style={{ width: NODE_WIDTH, height: NODE_HEIGHT }}
    >
      <Handle
        type="target"
        position={Position.Left}
        isConnectable={connectable}
        className={cn("!size-3 !border-card !bg-muted-foreground", !connectable && "!opacity-0")}
      />
      <div className="flex items-center gap-1.5">
        <span className="min-w-0 flex-1 truncate text-[0.8125rem] font-medium" title={asset.name}>
          {asset.name}
        </span>
        {flash ? (
          <span className="inline-flex shrink-0 items-center gap-0.5 rounded bg-warn-bg px-1 text-[0.6875rem] font-medium">
            <History aria-hidden className="size-3" />
            {flash === "undo" ? "已撤销" : "已重做"}
          </span>
        ) : locked ? (
          <span className="inline-flex shrink-0 items-center gap-0.5 text-[0.6875rem] text-muted-foreground">
            <Lock aria-hidden className="size-3" />
            已锁定
          </span>
        ) : null}
      </div>
      <div className="flex items-center justify-between gap-1.5 text-xs text-muted-foreground">
        <span className="truncate">{subtitle(asset, datacenter)}</span>
        <span className="shrink-0">{labelOf(ROLE_LABEL, asset.role)}</span>
      </div>
      <Handle
        type="source"
        position={Position.Right}
        isConnectable={connectable}
        className={cn("!size-3 !border-card !bg-muted-foreground", !connectable && "!opacity-0")}
      />
    </div>
  )
}
