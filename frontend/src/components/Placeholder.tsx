export default function Placeholder({ title, phase }: { title: string; phase: string }) {
  return (
    <div>
      <h1 className="text-2xl font-bold tracking-tight">{title}</h1>
      <div className="mt-8 rounded-lg border border-dashed border-slate-300 p-10 text-center dark:border-slate-700">
        <p className="text-slate-500 dark:text-slate-400">Coming in phase {phase} — see PLAN.md.</p>
      </div>
    </div>
  )
}
