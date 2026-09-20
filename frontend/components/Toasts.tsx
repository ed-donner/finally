import type { Toast } from "@/hooks/useToasts";

const STYLES: Record<Toast["kind"], string> = {
  success: "border-up/60 bg-up/10 text-up",
  error: "border-down/60 bg-down/10 text-down",
  info: "border-primary/60 bg-primary/10 text-primary",
};

/** Stacked, click-to-dismiss notifications pinned to the bottom-left of the viewport. */
export function Toasts({ toasts, onDismiss }: { toasts: Toast[]; onDismiss: (id: number) => void }) {
  return (
    <div className="pointer-events-none fixed bottom-4 left-4 z-50 flex max-w-sm flex-col gap-2" aria-live="polite">
      {toasts.map((t) => (
        <button
          key={t.id}
          data-testid="toast"
          onClick={() => onDismiss(t.id)}
          className={`pointer-events-auto rounded-md border px-3 py-2 text-left shadow-lg backdrop-blur ${STYLES[t.kind]}`}
          style={{ backgroundColor: "#161b22ee" }}
        >
          {t.message}
        </button>
      ))}
    </div>
  );
}
