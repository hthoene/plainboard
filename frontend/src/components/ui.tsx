import clsx from "clsx";
import { Check, ChevronDown, Loader2, X } from "lucide-react";
import {
  Children,
  forwardRef,
  isValidElement,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  type ButtonHTMLAttributes,
  type CSSProperties,
  type InputHTMLAttributes,
  type KeyboardEvent as ReactKeyboardEvent,
  type OptionHTMLAttributes,
  type ReactNode,
  type TextareaHTMLAttributes,
} from "react";
import { createPortal } from "react-dom";

type Variant = "primary" | "secondary" | "ghost" | "danger";

const variants: Record<Variant, string> = {
  primary: "bg-indigo-600 text-white hover:bg-indigo-500 disabled:bg-indigo-600/50",
  secondary:
    "bg-white text-zinc-800 ring-1 ring-zinc-200 hover:bg-zinc-50 dark:bg-zinc-900 dark:text-zinc-100 dark:ring-zinc-800 dark:hover:bg-zinc-800",
  ghost: "text-zinc-600 hover:bg-zinc-100 hover:text-zinc-900 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-100",
  danger: "bg-white text-red-600 ring-1 ring-red-200 hover:bg-red-50 dark:bg-zinc-900 dark:ring-red-900/60 dark:hover:bg-red-950/40",
};

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: Variant;
  size?: "sm" | "md";
  loading?: boolean;
  icon?: ReactNode;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(function Button(
  { variant = "secondary", size = "md", loading, icon, className, children, disabled, ...rest },
  ref,
) {
  return (
    <button
      ref={ref}
      className={clsx(
        "inline-flex items-center justify-center gap-1.5 rounded-md font-medium transition-colors focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-500 disabled:cursor-not-allowed disabled:opacity-60",
        size === "sm" ? "h-7 px-2.5 text-xs" : "h-8 px-3 text-sm",
        variants[variant],
        className,
      )}
      disabled={disabled || loading}
      {...rest}
    >
      {loading ? <Loader2 className="size-3.5 animate-spin" /> : icon}
      {children}
    </button>
  );
});

export function IconButton({ label, className, ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      className={clsx(
        "inline-flex size-7 items-center justify-center rounded-md text-zinc-500 transition-colors hover:bg-zinc-100 hover:text-zinc-900 disabled:opacity-40 dark:text-zinc-400 dark:hover:bg-zinc-800 dark:hover:text-zinc-100",
        className,
      )}
      {...rest}
    />
  );
}

// text-base on phones prevents iOS from zooming into focused inputs.
const fieldBase =
  "w-full rounded-md bg-white px-2.5 text-base sm:text-sm text-zinc-900 ring-1 ring-zinc-200 placeholder:text-zinc-400 focus:outline-none focus:ring-2 focus:ring-indigo-500 disabled:opacity-60 dark:bg-zinc-900 dark:text-zinc-100 dark:ring-zinc-800";

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function Input({ className, ...rest }, ref) {
  return <input ref={ref} className={clsx(fieldBase, "h-8", className)} {...rest} />;
});

export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(function Textarea(
  { className, ...rest },
  ref,
) {
  return <textarea ref={ref} className={clsx(fieldBase, "scroll-thin py-1.5 leading-relaxed", className)} {...rest} />;
});

const LIST_GUTTER = 8;
const LIST_GAP = 4;
const LIST_MAX_HEIGHT = 288;
const LIST_MIN_WIDTH = 160;

interface SelectOption {
  value: string;
  label: ReactNode;
  text: string;
  disabled: boolean;
}

function readOptions(children: ReactNode): SelectOption[] {
  return Children.toArray(children).flatMap((child) => {
    if (!isValidElement<OptionHTMLAttributes<HTMLOptionElement>>(child)) return [];
    const { value, children: label, disabled } = child.props;
    return [{ value: String(value ?? ""), label, text: Children.toArray(label).join("").toLowerCase(), disabled: !!disabled }];
  });
}

/** Styled replacement for <select>: browsers draw the native list themselves, so it cannot match the
 * app. Takes <option> children and calls onChange with `e.target.value` like the native element.
 * `trigger` replaces the field look, e.g. for a dropdown in a header. */
export function Select({
  value,
  onChange,
  children,
  className,
  disabled,
  trigger,
  "aria-label": ariaLabel,
}: {
  value?: string | number;
  onChange?: (e: { target: { value: string } }) => void;
  children: ReactNode;
  className?: string;
  disabled?: boolean;
  trigger?: ReactNode;
  "aria-label"?: string;
}) {
  const options = readOptions(children);
  const selected = Math.max(
    options.findIndex((o) => o.value === String(value ?? "")),
    0,
  );
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(0);
  const [position, setPosition] = useState<CSSProperties>({});
  const triggerRef = useRef<HTMLButtonElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const typed = useRef({ text: "", at: 0 });
  const id = useId();

  useLayoutEffect(() => {
    if (!open) return;
    const place = (e?: Event) => {
      const button = triggerRef.current;
      const list = listRef.current;
      if (!button || !list || e?.target === list) return;
      const rect = button.getBoundingClientRect();
      const below = window.innerHeight - rect.bottom - LIST_GUTTER - LIST_GAP;
      const above = rect.top - LIST_GUTTER - LIST_GAP;
      const up = list.scrollHeight > below && above > below;
      const width = Math.min(Math.max(rect.width, LIST_MIN_WIDTH), window.innerWidth - 2 * LIST_GUTTER);
      setPosition({
        left: Math.max(LIST_GUTTER, Math.min(rect.left, window.innerWidth - width - LIST_GUTTER)),
        width,
        maxHeight: Math.min(up ? above : below, LIST_MAX_HEIGHT),
        ...(up ? { bottom: window.innerHeight - rect.top + LIST_GAP } : { top: rect.bottom + LIST_GAP }),
      });
    };
    // Capture: also runs inside modals, which stop mouse events from bubbling further.
    const closeOutside = (e: PointerEvent) => {
      const target = e.target as Node;
      if (!triggerRef.current?.contains(target) && !listRef.current?.contains(target)) setOpen(false);
    };
    place();
    window.addEventListener("resize", place);
    window.addEventListener("scroll", place, true);
    document.addEventListener("pointerdown", closeOutside, true);
    return () => {
      window.removeEventListener("resize", place);
      window.removeEventListener("scroll", place, true);
      document.removeEventListener("pointerdown", closeOutside, true);
    };
  }, [open]);

  useEffect(() => {
    if (open) listRef.current?.children[active]?.scrollIntoView({ block: "nearest" });
  }, [open, active]);

  const show = () => {
    setActive(selected);
    setOpen(true);
    triggerRef.current?.focus();
  };

  const choose = (index: number) => {
    const option = options[index];
    if (!option || option.disabled) return;
    setOpen(false);
    if (index !== selected) onChange?.({ target: { value: option.value } });
  };

  const move = (start: number, step: number) => {
    for (let i = start; i >= 0 && i < options.length; i += step) {
      if (!options[i].disabled) return setActive(i);
    }
  };

  const typeAhead = (key: string) => {
    const now = Date.now();
    const text = (now - typed.current.at < 700 ? typed.current.text : "") + key.toLowerCase();
    typed.current = { text, at: now };
    const match = options.findIndex((o) => !o.disabled && o.text.startsWith(text));
    if (match >= 0) setActive(match);
  };

  const onKeyDown = (e: ReactKeyboardEvent<HTMLButtonElement>) => {
    if (!open) {
      if (["ArrowDown", "ArrowUp", "Enter", " "].includes(e.key)) {
        e.preventDefault();
        show();
      }
      return;
    }
    const keys: Record<string, () => void> = {
      ArrowDown: () => move(active + 1, 1),
      ArrowUp: () => move(active - 1, -1),
      Home: () => move(0, 1),
      End: () => move(options.length - 1, -1),
      Enter: () => choose(active),
      " ": () => choose(active),
      Escape: () => setOpen(false),
    };
    if (keys[e.key]) {
      e.preventDefault();
      e.stopPropagation(); // Escape closes the list, not the surrounding modal
      keys[e.key]();
    } else if (e.key === "Tab") setOpen(false);
    else if (e.key.length === 1) typeAhead(e.key);
  };

  return (
    <>
      <button
        ref={triggerRef}
        type="button"
        role="combobox"
        aria-haspopup="listbox"
        aria-expanded={open}
        aria-controls={open ? id : undefined}
        aria-activedescendant={open ? `${id}-${active}` : undefined}
        aria-label={ariaLabel}
        disabled={disabled}
        onClick={() => (open ? setOpen(false) : show())}
        onKeyDown={onKeyDown}
        onBlur={() => setOpen(false)}
        className={clsx(
          trigger
            ? "block w-full cursor-pointer rounded-md text-left focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-indigo-500"
            : clsx(fieldBase, "flex h-8 cursor-pointer items-center justify-between gap-2 text-left disabled:cursor-not-allowed", open && "ring-2 ring-indigo-500"),
          className,
        )}
      >
        {trigger ?? (
          <>
            <span className="min-w-0 truncate">{options[selected]?.label}</span>
            <ChevronDown className={clsx("size-3.5 shrink-0 text-zinc-400 transition-transform", open && "rotate-180")} />
          </>
        )}
      </button>
      {open &&
        createPortal(
          <div
            ref={listRef}
            id={id}
            role="listbox"
            style={position}
            // Keeps focus on the trigger; the portal must not trigger handlers of the surrounding modal.
            onMouseDown={(e) => {
              e.preventDefault();
              e.stopPropagation();
            }}
            onClick={(e) => e.stopPropagation()}
            className="scroll-thin fixed z-[60] overflow-y-auto rounded-lg bg-white p-1 text-base text-zinc-900 shadow-lg ring-1 ring-zinc-200 sm:text-sm dark:bg-zinc-900 dark:text-zinc-100 dark:ring-zinc-800"
          >
            {options.map((option, index) => (
              <div
                key={option.value}
                id={`${id}-${index}`}
                role="option"
                aria-selected={index === selected}
                aria-disabled={option.disabled || undefined}
                onMouseMove={() => !option.disabled && setActive(index)}
                onClick={() => choose(index)}
                className={clsx(
                  "flex items-center gap-2 rounded-md px-2 py-2 sm:py-1.5",
                  option.disabled ? "opacity-50" : "cursor-pointer",
                  index === active && !option.disabled && "bg-zinc-100 dark:bg-zinc-800",
                  index === selected && "font-medium",
                )}
              >
                <span className="min-w-0 flex-1 truncate">{option.label}</span>
                {index === selected && <Check className="size-3.5 shrink-0 text-indigo-600 dark:text-indigo-400" />}
              </div>
            ))}
          </div>,
          document.body,
        )}
    </>
  );
}

/** Labelled form field. Use `group` for composite content (buttons, lists, toggles): inside a
 * <label>, clicking any button would also "click" the first control of the label. */
export function Field({
  label,
  hint,
  children,
  className,
  group,
}: {
  label: string;
  hint?: ReactNode;
  children: ReactNode;
  className?: string;
  group?: boolean;
}) {
  const Tag = group ? "div" : "label";
  return (
    <Tag className={clsx("block space-y-1", className)} {...(group ? { role: "group", "aria-label": label } : {})}>
      <span className="block text-xs font-medium text-zinc-600 dark:text-zinc-400">{label}</span>
      {children}
      {hint && <span className="block text-xs text-zinc-500">{hint}</span>}
    </Tag>
  );
}

export function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => void; label: ReactNode }) {
  return (
    <label className="flex cursor-pointer items-center gap-2 text-sm select-none">
      <button
        type="button"
        role="switch"
        aria-checked={checked}
        onClick={() => onChange(!checked)}
        className={clsx(
          "relative h-5 w-9 shrink-0 rounded-full transition-colors",
          checked ? "bg-indigo-600" : "bg-zinc-300 dark:bg-zinc-700",
        )}
      >
        <span className={clsx("absolute top-0.5 size-4 rounded-full bg-white shadow transition-all", checked ? "left-4.5" : "left-0.5")} />
      </button>
      <span>{label}</span>
    </label>
  );
}

export function Modal({
  open,
  onClose,
  title,
  children,
  footer,
  width = "max-w-2xl",
}: {
  open: boolean;
  onClose: () => void;
  title: ReactNode;
  children: ReactNode;
  footer?: ReactNode;
  width?: string;
}) {
  useEffect(() => {
    if (!open) return;
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose();
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);
  if (!open) return null;
  return (
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-zinc-950/40 backdrop-blur-[2px] sm:overflow-y-auto sm:p-4 sm:pt-[6vh]" onMouseDown={onClose}>
      <div
        role="dialog"
        aria-modal="true"
        className={clsx(
          "flex h-full w-full flex-col bg-white shadow-2xl ring-1 ring-zinc-200 sm:h-auto sm:max-h-[88vh] sm:rounded-xl dark:bg-zinc-900 dark:ring-zinc-800",
          "pb-[env(safe-area-inset-bottom)]",
          width,
        )}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between gap-3 border-b border-zinc-100 px-5 py-3 dark:border-zinc-800">
          <div className="min-w-0 flex-1 text-base font-semibold">{title}</div>
          <IconButton label="Close" onClick={onClose}>
            <X className="size-4" />
          </IconButton>
        </div>
        <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
        {footer && <div className="flex items-center justify-end gap-2 border-t border-zinc-100 px-5 py-3 dark:border-zinc-800">{footer}</div>}
      </div>
    </div>
  );
}

export function Badge({ children, tone = "zinc", className }: { children: ReactNode; tone?: "zinc" | "indigo" | "green" | "amber" | "red" | "sky"; className?: string }) {
  const tones = {
    zinc: "bg-zinc-100 text-zinc-700 dark:bg-zinc-800 dark:text-zinc-300",
    indigo: "bg-indigo-50 text-indigo-700 dark:bg-indigo-950/60 dark:text-indigo-300",
    green: "bg-emerald-50 text-emerald-700 dark:bg-emerald-950/50 dark:text-emerald-300",
    amber: "bg-amber-50 text-amber-700 dark:bg-amber-950/50 dark:text-amber-300",
    red: "bg-red-50 text-red-700 dark:bg-red-950/50 dark:text-red-300",
    sky: "bg-sky-50 text-sky-700 dark:bg-sky-950/50 dark:text-sky-300",
  };
  return <span className={clsx("inline-flex items-center gap-1 rounded px-1.5 py-0.5 text-[11px] font-medium", tones[tone], className)}>{children}</span>;
}

export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={clsx("size-4 animate-spin text-zinc-400", className)} />;
}

export function EmptyState({ icon, title, children }: { icon?: ReactNode; title: string; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-12 text-center text-zinc-500">
      {icon && <div className="text-zinc-300 dark:text-zinc-700">{icon}</div>}
      <div className="text-sm font-medium text-zinc-700 dark:text-zinc-300">{title}</div>
      {children && <div className="max-w-sm text-sm">{children}</div>}
    </div>
  );
}

export function ErrorText({ error }: { error: unknown }) {
  if (!error) return null;
  return <p className="text-sm text-red-600 dark:text-red-400">{error instanceof Error ? error.message : String(error)}</p>;
}

export function AgentDot({ color, status, className }: { color: string; status?: string; className?: string }) {
  return (
    <span className={clsx("relative inline-flex size-2.5 shrink-0", className)}>
      {status === "running" && <span className="absolute inline-flex size-full animate-ping rounded-full opacity-60" style={{ background: color }} />}
      <span
        className={clsx("relative inline-flex size-2.5 rounded-full", status === "queued" && "ring-2 ring-amber-400/60")}
        style={{ background: color }}
      />
    </span>
  );
}
