import { useRef, type CSSProperties } from "react"
import { CheckIcon, ChevronDownIcon, CircleHelpIcon } from "lucide-react"
import { DropdownMenu as Menu, Popover, Slider } from "radix-ui"
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip"
import type { EffortInfo, ModelCatalog, ModelChoice, ModelInfo } from "@/lib/api"
import { cn } from "@/lib/utils"

const RECOMMENDED = "medium"
// As the other popups of the app: they stay on the screen at each zoom level, and animate in and out from their trigger.
const POPUP = "z-50 overflow-y-auto rounded-lg bg-popover text-popover-foreground shadow-md ring-1 ring-foreground/10 outline-none duration-100 data-[side=bottom]:slide-in-from-top-2 data-[side=top]:slide-in-from-bottom-2 data-[state=open]:animate-in data-[state=open]:fade-in-0 data-[state=open]:zoom-in-95 data-[state=closed]:animate-out data-[state=closed]:fade-out-0 data-[state=closed]:zoom-out-95"
const EDGE = 8 // the space between a popup and the edge of the window, in px
const ITEM = "flex cursor-pointer items-start gap-2 rounded-md px-2 py-1.5 outline-none select-none data-[highlighted]:bg-muted"

type Props = {
  models: ModelInfo[]
  efforts: EffortInfo[]
  model: string
  effort: string
  onChange: (model: string, effort: string) => void
  /** A line under the models, e.g. that the goals of a mandate use the model of its chat. */
  note?: string
  /** Set while the agents work: the reason that the model and the effort cannot change now. */
  locked?: string
}

/**
 * The model and the effort of a goal, under the message box, as in Claude: the model name opens the models that the
 * SDK gives, and the effort chip opens a Faster to Smarter slider. The slider has only the levels of the model, and a model
 * without levels has no chip. The track fills with dots that move faster and glow more at each higher level.
 */
function Picker({ models, efforts, model, effort, onChange, note, locked }: Props) {
  const current = models.find((m) => m.id === model)
  const levels = levelsOf(current, efforts)
  const choose = (id: string) => onChange(id, fitEffort(levelsOf(models.find((m) => m.id === id), efforts), efforts, effort))
  const index = Math.max(0, levels.findIndex((e) => e.id === effort))
  const level = Math.max(0, efforts.findIndex((e) => e.id === levels[index]?.id)) // for the colour: Max is always 4
  const label = levels[index]?.label ?? effort
  const last = levels.length - 1
  const thumb = useRef<HTMLSpanElement>(null)
  const picker = (
    <div className="flex items-center gap-0.5">
      <Menu.Root>
        <Menu.Trigger
          disabled={Boolean(locked)}
          aria-label={`Model: ${current?.label ?? model}`}
          className="flex h-8 items-center gap-1 rounded-lg px-2 text-sm text-muted-foreground outline-none hover:bg-muted hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-60 data-[state=open]:bg-muted data-[state=open]:text-foreground"
        >
          {current?.label ?? model}
          <ChevronDownIcon aria-hidden className="size-3.5 opacity-60" />
        </Menu.Trigger>
        <Menu.Portal>
          <Menu.Content align="end" sideOffset={6} collisionPadding={EDGE}
            className={cn(POPUP, "w-64 max-w-(--radix-dropdown-menu-content-available-width) max-h-(--radix-dropdown-menu-content-available-height) origin-(--radix-dropdown-menu-content-transform-origin) p-1")}>
            <Menu.RadioGroup value={model} onValueChange={choose}>
              {models.map((m) => <ModelItem key={m.id} model={m} />)}
            </Menu.RadioGroup>
            {note && <p className="mt-1 border-t px-2 pt-2 pb-1 text-xs text-muted-foreground">{note}</p>}
          </Menu.Content>
        </Menu.Portal>
      </Menu.Root>

      {levels.length > 0 && <Popover.Root>
        <Popover.Trigger
          disabled={Boolean(locked)}
          aria-label={`Effort: ${label}`}
          data-level={level}
          className="effort-chip h-7 rounded-md bg-muted px-2 text-xs font-medium text-foreground/80 outline-none hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring disabled:pointer-events-none disabled:opacity-60"
        >
          {label}
        </Popover.Trigger>
        <Popover.Portal>
          <Popover.Content align="end" side="top" sideOffset={6} collisionPadding={EDGE}
            // Focus the slider, not the help icon: the arrow keys then change the effort, and the help does not open.
            onOpenAutoFocus={(event) => { event.preventDefault(); thumb.current?.focus() }}
            className={cn(POPUP, "w-64 max-w-(--radix-popover-content-available-width) max-h-(--radix-popover-content-available-height) origin-(--radix-popover-content-transform-origin) p-3")}>
            <div className="flex items-center gap-2">
              <span className="text-sm text-muted-foreground">Effort</span>
              <span className="effort-label text-sm font-semibold" data-level={level}>{label}</span>
              <Tooltip>
                <TooltipTrigger asChild>
                  <button type="button" aria-label="What effort does" className="ml-auto text-muted-foreground hover:text-foreground">
                    <CircleHelpIcon className="size-3.5" />
                  </button>
                </TooltipTrigger>
                <TooltipContent className="max-w-60">
                  Higher effort makes the agents think longer. The results are more careful, but slower and more costly.
                </TooltipContent>
              </Tooltip>
            </div>
            <div className="mt-3 flex justify-between text-xs text-muted-foreground">
              <span>Faster</span>
              <span>Smarter</span>
            </div>
            <Slider.Root
              min={0} max={last} step={1} value={[index]}
              onValueChange={([i]) => levels[i] && levels[i].id !== effort && onChange(model, levels[i].id)}
              className="relative mt-1.5 flex h-6 w-full touch-none items-center select-none"
            >
              <Slider.Track className="relative h-full grow overflow-hidden rounded-md bg-muted">
                <Slider.Range className="effort-fill absolute inset-y-0" data-level={level} />
                {levels.map((e, i) => (
                  // A mark at each stop. The mark of the recommended level is a short bar.
                  <span key={e.id} aria-hidden
                    className={cn("absolute top-1/2 -translate-1/2 rounded-full bg-foreground/35",
                      e.id === RECOMMENDED ? "h-2.5 w-0.5" : "size-1")}
                    style={{ left: stop(i, last) } as CSSProperties} />
                ))}
              </Slider.Track>
              <Slider.Thumb ref={thumb} aria-label="Effort" aria-valuetext={label}
                className="block h-5 w-3.5 rounded-sm bg-white shadow-md ring-1 ring-black/10 outline-none focus-visible:ring-2 focus-visible:ring-ring" />
            </Slider.Root>
            <div className="relative mt-1 h-4 text-xs text-muted-foreground">
              {levels.some((e) => e.id === RECOMMENDED) && (
                <span className="absolute -translate-x-1/2" style={{ left: stop(levels.findIndex((e) => e.id === RECOMMENDED), last) }}>
                  Recommended
                </span>
              )}
            </div>
          </Popover.Content>
        </Popover.Portal>
      </Popover.Root>}
    </div>
  )
  if (!locked) return picker
  // Disabled buttons get no pointer events: the wrapper shows the reason.
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span data-testid="model-lock" tabIndex={0} className="rounded-lg outline-none focus-visible:ring-2 focus-visible:ring-ring">{picker}</span>
      </TooltipTrigger>
      <TooltipContent>{locked}</TooltipContent>
    </Tooltip>
  )
}

type PickerProps = {
  catalog: ModelCatalog
  choice: ModelChoice
  onChange: (choice: ModelChoice) => void
  note?: string
  locked?: string
}

export function ModelPicker({ catalog, choice, onChange, note, locked }: PickerProps) {
  return <Picker models={catalog.models} efforts={catalog.efforts} model={choice.model} effort={choice.effort} note={note}
    locked={locked} onChange={(model, effort) => onChange({ model, effort })} />
}

/** The effort levels that the SDK gives for a model. A model that is not in the list, e.g. an earlier choice of an
 * old goal, has all levels. */
function levelsOf(model: ModelInfo | undefined, efforts: EffortInfo[]): EffortInfo[] {
  return model?.efforts ? efforts.filter((e) => model.efforts!.includes(e.id)) : efforts
}

/** The effort for a newly selected model: the same level, or the next lower level that the model has. A model
 * without levels keeps the effort: the server ignores it. */
function fitEffort(levels: EffortInfo[], efforts: EffortInfo[], effort: string): string {
  if (levels.length === 0 || levels.some((e) => e.id === effort)) return effort
  const rank = (id: string) => efforts.findIndex((e) => e.id === id) // the server gives the levels from low to max
  const lower = levels.filter((e) => rank(e.id) <= rank(effort))
  return (lower.at(-1) ?? levels[0]).id
}

function ModelItem({ model }: { model: ModelInfo }) {
  return (
    <Menu.RadioItem value={model.id} className={ITEM}>
      <span className="flex min-w-0 flex-1 flex-col">
        <span className="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-sm font-medium">
          {model.label}
          {model.badge && <span className="rounded bg-muted px-1.5 py-px text-[0.6875rem] font-normal text-muted-foreground">{model.badge}</span>}
        </span>
        <span className="text-xs text-muted-foreground">{model.detail}</span>
      </span>
      <Menu.ItemIndicator className="mt-0.5 text-primary"><CheckIcon className="size-4" /></Menu.ItemIndicator>
    </Menu.RadioItem>
  )
}

/** The place of stop i on the track, as the slider puts its thumb there: the thumb (w-3.5, 0.875 rem) stays inside
 * the track. In rem, so that the marks stay under the thumb when the user makes the text larger. */
function stop(i: number, last: number): string {
  const part = last > 0 ? i / last : 0
  return `calc(${part * 100}% + ${0.4375 - part * 0.875}rem)`
}
