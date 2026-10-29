import { cn } from '@/lib/utils'

export function Kbd({ className, ...props }: React.ComponentProps<'kbd'>) {
  return (
    <kbd
      className={cn(
        'inline-flex h-[18px] items-center whitespace-nowrap rounded border border-border bg-muted px-1.5 font-mono text-[10.5px] font-medium text-muted-foreground',
        className,
      )}
      {...props}
    />
  )
}
