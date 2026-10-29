import { cn } from '@/lib/utils'

/** Box with a slowly rotating violet→cyan conic-gradient border (Aceternity style). */
export function GlowBorder({ className, children }: { className?: string; children: React.ReactNode }) {
  return <div className={cn('glow-border rounded-xl', className)}>{children}</div>
}
