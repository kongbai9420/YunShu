import type { ReactNode } from "react"
import { DndContext, KeyboardSensor, PointerSensor, closestCenter, useSensor, useSensors } from "@dnd-kit/core"
import { SortableContext, arrayMove, rectSortingStrategy, sortableKeyboardCoordinates, useSortable } from "@dnd-kit/sortable"
import { CSS } from "@dnd-kit/utilities"
import { ArrowDown, ArrowUp, GripVertical } from "lucide-react"

import { Button } from "@/components/ui/button"

function Item({ id, name, editing, first, last, move, children }: {
  id: string; name: string; editing: boolean; first: boolean; last: boolean
  move: (direction: -1 | 1) => void; children: (controls: ReactNode) => ReactNode
}) {
  const { attributes, listeners, setNodeRef, transform, transition, isDragging } = useSortable({ id, disabled: !editing })
  return <div ref={setNodeRef} data-sort-id={id} className="min-w-0" style={{
    transform: CSS.Transform.toString(transform), transition,
    position: "relative", zIndex: isDragging ? 10 : undefined, opacity: isDragging ? 0.8 : 1,
  }}>
    {children(editing ? <span className="inline-flex shrink-0 items-center gap-1">
      <button type="button" className="inline-flex size-8 touch-none items-center justify-center rounded-md text-muted-foreground hover:bg-accent"
        aria-label={`拖动${name}`} {...attributes} {...listeners}><GripVertical aria-hidden className="size-4" /></button>
      <Button size="icon-xs" variant="ghost" aria-label={`上移${name}`} disabled={first} onClick={() => move(-1)}><ArrowUp aria-hidden /></Button>
      <Button size="icon-xs" variant="ghost" aria-label={`下移${name}`} disabled={last} onClick={() => move(1)}><ArrowDown aria-hidden /></Button>
    </span> : null)}
  </div>
}

/** 每个列表只接受当前同级 ID，拖动不能改业务关系；筛选隐藏项的顺序保留。 */
export function SortableList({ ids, allIds = ids, nameOf, editing, onChange, children, className = "space-y-3" }: {
  ids: string[]; allIds?: string[]; nameOf: (id: string) => string; editing: boolean
  onChange: (ids: string[]) => void; children: (id: string, controls: ReactNode) => ReactNode; className?: string
}) {
  const sensors = useSensors(useSensor(PointerSensor, { activationConstraint: { distance: 5 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }))
  const reorder = (from: number, to: number) => {
    if (from < 0 || to < 0 || to >= ids.length || from === to) return
    const visible = arrayMove(ids, from, to)
    const positions = new Set(ids)
    let cursor = 0
    onChange(allIds.map((id) => positions.has(id) ? visible[cursor++] : id))
  }
  return <DndContext sensors={sensors} collisionDetection={closestCenter} onDragEnd={({ active, over }) => {
    if (over) reorder(ids.indexOf(String(active.id)), ids.indexOf(String(over.id)))
  }} accessibility={{
    screenReaderInstructions: { draggable: "按空格拿起，方向键移动，再按空格放下；Esc 取消。只能调整当前同级显示顺序。" },
    announcements: {
      onDragStart: ({ active }) => `已拿起${nameOf(String(active.id))}`,
      onDragOver: ({ over }) => over ? `移到${nameOf(String(over.id))}的位置` : "",
      onDragEnd: ({ over }) => over ? "已调整显示顺序，尚未保存" : "未改变顺序",
      onDragCancel: () => "已取消拖动",
    },
  }}>
    <SortableContext items={ids} strategy={rectSortingStrategy}>
      <div className={className}>{ids.map((id, index) => <Item key={id} id={id} name={nameOf(id)} editing={editing}
        first={index === 0} last={index === ids.length - 1} move={(d) => reorder(index, index + d)}>
        {(controls) => children(id, controls)}
      </Item>)}</div>
    </SortableContext>
  </DndContext>
}
