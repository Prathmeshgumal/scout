import { Loader2 } from 'lucide-react'
import { Button } from '@/components/ui/button'
import { cn } from '@/lib/utils'

type Props = React.ComponentProps<typeof Button> & { loading?: boolean }

/** Violet gradient button with a moving shimmer and soft glow (Magic UI style). */
export function ShimmerButton({ loading = false, disabled, className, children, ...props }: Props) {
  return (
    <Button
      disabled={disabled || loading}
      aria-busy={loading}
      className={cn(
        'glow-shimmer h-8 gap-1.5 border-white/15 px-3 text-white shadow-[0_0_24px_-4px_rgba(168,85,247,0.6)] hover:brightness-110 disabled:opacity-60',
        className,
      )}
      {...props}
    >
      {loading && <Loader2 className="size-3.5 animate-spin" aria-hidden />}
      {children}
    </Button>
  )
}
