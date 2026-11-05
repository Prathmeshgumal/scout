import ReactMarkdown, { type Components } from 'react-markdown'
import remarkGfm from 'remark-gfm'

// Compact styling for chat replies (no typography plugin in this project).
const components: Components = {
  p: (props) => <p className="my-2 first:mt-0 last:mb-0" {...props} />,
  ul: (props) => <ul className="my-2 list-disc space-y-1 pl-5" {...props} />,
  ol: (props) => <ol className="my-2 list-decimal space-y-1 pl-5" {...props} />,
  h1: (props) => <h3 className="mt-3 mb-1.5 font-semibold" {...props} />,
  h2: (props) => <h3 className="mt-3 mb-1.5 font-semibold" {...props} />,
  h3: (props) => <h3 className="mt-3 mb-1.5 font-semibold" {...props} />,
  a: (props) => <a className="text-violet-600 underline underline-offset-2 dark:text-violet-300" target="_blank" rel="noreferrer" {...props} />,
  pre: (props) => (
    <pre
      className="my-2 overflow-x-auto rounded-lg border bg-muted/60 p-3 font-mono text-[12px] leading-relaxed [&_code]:bg-transparent [&_code]:p-0"
      {...props}
    />
  ),
  code: (props) => <code className="rounded bg-muted px-1 py-0.5 font-mono text-[12px]" {...props} />,
  table: (props) => <table className="my-2 w-full border-collapse text-[12px] [&_td]:border [&_td]:px-2 [&_th]:border [&_th]:px-2" {...props} />,
}

export function Markdown({ children }: { children: string }) {
  return (
    <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
      {children}
    </ReactMarkdown>
  )
}
