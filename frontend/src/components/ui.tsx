import clsx from "clsx";
import { useTranslation } from "react-i18next";
import { ShellContext } from "../features/layout/shell-context";
import { Check, ChevronDown, Loader2, PanelRight, X } from "lucide-react";
import {
  Children,
  forwardRef,
  isValidElement,
  useEffect,
  useId,
  useLayoutEffect,
  useRef,
  useState,
  useContext,
  type ButtonHTMLAttributes,
  type CSSProperties,
  type InputHTMLAttributes,
  type KeyboardEvent as ReactKeyboardEvent,
  type OptionHTMLAttributes,
  type ReactNode,
  type TextareaHTMLAttributes,
} from "react";
import { createPortal } from "react-dom";
import { useAutoGrow, useOptimistic } from "../lib/hooks";

type Variant = "primary" | "secondary" | "ghost" | "danger";

const variants: Record<Variant, string> = {
  primary: "plate hover:bg-fg-2 disabled:hover:bg-fg",
  secondary:
    "font-medium text-fg ring-1 ring-inset ring-line-2 hover:bg-sunk",
  ghost: "font-medium text-fg-2 hover:bg-sunk hover:text-fg",
  danger: "font-medium text-fg-2 ring-1 ring-inset ring-line-2 hover:bg-sunk hover:text-fg",
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
      data-part="button"
      data-variant={variant}
      className={clsx(
        "inline-flex shrink-0 items-center justify-center gap-1.5 rounded-sm whitespace-nowrap transition-colors disabled:cursor-not-allowed disabled:opacity-60",
        size === "sm" ? "h-7 px-2.5 touch:h-11" : "h-8 px-3 touch:h-11",
        variants[variant],
        className,
      )}
      disabled={disabled || loading}
      {...rest}
    >
      {loading ? <Loader2 className="size-3.5 animate-spin" /> : icon}
      {labelled(children)}
    </button>
  );
});

/** Text that stands directly in a button gets a box of its capital letters (`label-line`), so it sits
    in the middle of the button like the icon beside it. Text next to text stays one piece. */
function labelled(children: ReactNode): ReactNode {
  const out: ReactNode[] = [];
  let text: (string | number)[] = [];
  const close = () => {
    // Only spaces between two elements: no piece of its own, it would widen the gap.
    if (text.join("").trim()) {
      out.push(
        <span key={`label-${out.length}`} className="label-line">
          {text}
        </span>,
      );
    }
    text = [];
  };
  for (const child of Children.toArray(children)) {
    if (typeof child === "string" || typeof child === "number") text.push(child);
    else {
      close();
      out.push(child);
    }
  }
  close();
  return out;
}

export function IconButton({ label, className, ...rest }: ButtonHTMLAttributes<HTMLButtonElement> & { label: string }) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      className={clsx(
        "inline-flex size-7 shrink-0 items-center justify-center rounded-sm text-fg-3 transition-colors hover:bg-sunk hover:text-fg disabled:opacity-40 touch:size-11",
        className,
      )}
      {...rest}
    />
  );
}

// text-base on phones prevents iOS from zooming into focused inputs.
const fieldBase =
  "w-full rounded-sm bg-transparent px-2.5 text-fg ring-1 ring-inset ring-line-2 placeholder:text-fg-3 focus:outline-none focus:ring-2 focus:ring-fg disabled:opacity-60";

export const Input = forwardRef<HTMLInputElement, InputHTMLAttributes<HTMLInputElement>>(function Input({ className, ...rest }, ref) {
  return <input ref={ref} data-part="field" className={clsx(fieldBase, "h-8 touch:h-11", className)} {...rest} />;
});

/** Multi-line field. Never resized by hand: it grows with its text from `rows` up to `max-h-[60vh]`
 * (a `max-h-*` in `className` replaces that), then it scrolls. */
export const Textarea = forwardRef<HTMLTextAreaElement, TextareaHTMLAttributes<HTMLTextAreaElement>>(function Textarea(
  { className, ...rest },
  ref,
) {
  const own = useRef<HTMLTextAreaElement | null>(null);
  useAutoGrow(own, rest.value);
  const setRef = (el: HTMLTextAreaElement | null) => {
    own.current = el;
    if (typeof ref === "function") ref(el);
    else if (ref) ref.current = el;
  };
  return (
    <textarea
      ref={setRef}
      data-part="field"
      className={clsx(fieldBase, "scroll-thin resize-none py-1.5 leading-relaxed", !/(^|\s)max-h-/.test(className ?? "") && "max-h-[60vh]", className)}
      {...rest}
    />
  );
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
        data-part={trigger ? undefined : "field"}
        className={clsx(
          trigger
            ? "block w-full cursor-pointer rounded-sm text-left"
            : clsx(fieldBase, "flex h-8 cursor-pointer items-center justify-between gap-2 text-left disabled:cursor-not-allowed touch:h-11", open && "ring-2 ring-fg"),
          className,
        )}
      >
        {trigger ?? (
          <>
            <span className="min-w-0 truncate">{options[selected]?.label}</span>
            <ChevronDown className={clsx("size-3.5 shrink-0 text-fg-3 transition-transform", open && "rotate-180")} />
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
            data-part="menu"
            className="scroll-thin fixed z-[60] overflow-y-auto rounded-sm bg-bg p-1 text-fg shadow-lg ring-1 ring-line-2"
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
                  "flex items-center gap-2 rounded-sm px-2 py-1.5 touch:py-3",
                  option.disabled ? "opacity-50" : "cursor-pointer",
                  index === active && !option.disabled && "bg-sunk",
                  index === selected && "font-semibold",
                )}
              >
                <span className="min-w-0 flex-1 truncate">{option.label}</span>
                {index === selected && <Check className="size-3.5 shrink-0 text-fg" />}
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
  // Spaced by "what follows another", not by "all but the last" (space-y): an element a password
  // manager adds after the input must not move what is below it.
  return (
    <Tag className={clsx("block [&>*+*]:mt-1", className)} {...(group ? { role: "group", "aria-label": label } : {})}>
      <span className="block self-end font-medium text-fg-2">{label}</span>
      {children}
      {hint && <span className="block text-fg-3">{hint}</span>}
    </Tag>
  );
}

/** Two fields side by side. Their labels, inputs and hints share the rows, so the inputs stay on one
 * line when one label wraps. */
export function FieldRow({ children }: { children: ReactNode }) {
  return <div className="grid gap-3 sm:grid-cols-2 sm:gap-y-0 sm:*:row-span-3 sm:*:grid sm:*:grid-rows-subgrid">{children}</div>;
}

/** The look of an on/off switch, shared by every switch (Toggle, and switches inside menu items). Each
 * design styles it through data-part="toggle" and data-on. */
export function SwitchTrack({ checked }: { checked: boolean }) {
  return (
    <span data-part="toggle" data-on={checked} aria-hidden className={clsx("relative h-5 w-9 shrink-0 rounded-sm ring-1 ring-inset transition-colors", checked ? "bg-fg ring-fg" : "ring-line-2")}>
      <span className={clsx("absolute top-1 size-3 rounded-[1px] transition-all", checked ? "left-5 bg-bg" : "left-1 bg-fg-3")} />
    </span>
  );
}

/** An on/off switch. An `onChange` that saves returns its promise: the switch flips at once, and flips back
 * with the error shown when the save fails (#188). */
export function Toggle({ checked, onChange, label }: { checked: boolean; onChange: (v: boolean) => unknown; label: ReactNode }) {
  const [shown, change] = useOptimistic(checked);
  const [error, setError] = useState<unknown>(null);
  const flip = () => {
    setError(null);
    change(!shown, onChange).catch(setError);
  };
  return (
    <>
      <label className="flex cursor-pointer items-center gap-2 select-none">
        <button type="button" role="switch" aria-checked={shown} onClick={flip} className="flex shrink-0">
          <SwitchTrack checked={shown} />
        </button>
        {/* Trim the text box to cap height and baseline so the letters, not the line box, are centred on the switch in every design's font. */}
        <span className="[text-box:trim-both_cap_alphabetic]">{label}</span>
      </label>
      <ErrorText error={error} />
    </>
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
    <div className="fixed inset-0 z-50 flex items-start justify-center bg-black/55 sm:overflow-y-auto sm:p-4 sm:pt-[6vh]" onMouseDown={onClose}>
      <div
        role="dialog"
        data-part="modal"
        aria-modal="true"
        className={clsx(
          "flex h-full w-full flex-col bg-bg shadow-lg sm:h-auto sm:max-h-[88vh] sm:rounded-sm sm:ring-1 sm:ring-line-2",
          "pt-[env(safe-area-inset-top)] pb-[env(safe-area-inset-bottom)]",
          width,
        )}
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div data-part="modal-head" className="mx-4 flex items-center justify-between gap-3 rule-b py-2.5 sm:mx-5">
          <div className="min-w-0 flex-1 font-extrabold">{title}</div>
          <IconButton label="Close" onClick={onClose}>
            <X className="size-4" />
          </IconButton>
        </div>
        <div className="scroll-thin min-h-0 flex-1 overflow-y-auto px-4 py-4 sm:px-5">{children}</div>
        {footer && <div className="flex flex-wrap items-center justify-end gap-2 border-t border-line px-4 py-3 sm:px-5">{footer}</div>}
      </div>
    </div>
  );
}

export function Badge({ children, tone = "zinc", className }: { children: ReactNode; tone?: "zinc" | "indigo" | "green" | "amber" | "red" | "sky"; className?: string }) {
  const tones = {
    zinc: "text-fg-2 ring-line-2",
    indigo: "text-fg ring-fg",
    green: "text-ok ring-line-2",
    amber: "bg-you-bg text-you-fg ring-you",
    red: "text-fg-2 ring-line-2",
    sky: "text-fg-2 ring-line-2",
  };
  return <span data-part="badge" data-tone={tone} className={clsx("inline-flex items-center gap-1 rounded-sm px-1.5 whitespace-nowrap ring-1 ring-inset", tones[tone], className)}>{children}</span>;
}

export function Spinner({ className }: { className?: string }) {
  return <Loader2 className={clsx("size-4 animate-spin text-fg-3", className)} />;
}

export function EmptyState({ icon, title, children }: { icon?: ReactNode; title: string; children?: ReactNode }) {
  return (
    <div className="flex flex-col items-center justify-center gap-2 px-6 py-12 text-center text-fg-3">
      {icon && <div className="text-fg-3">{icon}</div>}
      <div className="font-medium text-fg">{title}</div>
      {children && <div className="max-w-sm">{children}</div>}
    </div>
  );
}

export function ErrorText({ error }: { error: unknown }) {
  if (!error) return null;
  return <p data-part="error" className="text-you-fg">{error instanceof Error ? error.message : String(error)}</p>;
}

/** State mark of an agent in a fixed cell: filled = working, hollow = queued or a background job,
 * empty = idle. */
export function AgentDot({ status, className }: { color?: string; status?: string; className?: string }) {
  return <span className={clsx("mk", status === "running" ? "mk-run" : status === "queued" || status === "background" ? "mk-plan" : "", className)} />;
}

/** The agent's letter on its colour. */
export function AgentSquare({ agent, className }: { agent: { name: string; color: string }; className?: string }) {
  return (
    <span
      title={agent.name}
      data-part="agent"
      className={clsx("inline-flex size-[18px] shrink-0 items-center justify-center rounded-sm text-[11.5px] leading-none font-extrabold text-white", className)}
      style={{ background: agent.color }}
    >
      {agent.name[0]?.toUpperCase()}
    </span>
  );
}

/** The bar every view starts with: 48px high, title at the left, actions at the right. */
/**
 * Shows or hides the side chat. It sits at the far right of the header of the view and, while the chat is open,
 * at the same spot in the chat's own header; `where` says which of the two this one is.
 */
export function ChatPanelToggle({ where, divider }: { where: "view" | "chat"; divider?: boolean }) {
  const { t } = useTranslation();
  const panel = useContext(ShellContext)?.panel;
  if (!panel?.available || (where === "view" && panel.open)) return null;
  return (
    <>
      {divider && <span aria-hidden className="mx-1 h-5 w-px shrink-0 bg-line-2" />}
      <IconButton label={t("nav.togglePanel")} aria-pressed={panel.open} onClick={panel.toggle} className={clsx(panel.open && "bg-sunk text-fg")}>
        <PanelRight className="size-4" />
      </IconButton>
    </>
  );
}

export function PageHead({ title, children }: { title: ReactNode; children?: ReactNode }) {
  return (
    <header data-part="page-head" className="flex h-12 shrink-0 items-center gap-2 border-b border-line px-4">
      <h1 className="min-w-0 truncate font-extrabold">{title}</h1>
      <div className="flex-1" />
      {children}
      <ChatPanelToggle where="view" divider />
    </header>
  );
}

/** A choice between a few options, as one joined control; the chosen one is the plate. */
export function Segmented<T extends string | number>({
  value,
  onChange,
  options,
  className,
  "aria-label": ariaLabel,
}: {
  value: T;
  /** One that saves returns its promise: the choice shows at once, and goes back with the error shown when it fails. */
  onChange: (value: T) => unknown;
  options: { value: T; label: ReactNode }[];
  className?: string;
  "aria-label"?: string;
}) {
  const [shown, change] = useOptimistic(value);
  const [error, setError] = useState<unknown>(null);
  const choose = (next: T) => {
    setError(null);
    change(next, onChange).catch(setError);
  };
  return (
    <>
      <div role="group" aria-label={ariaLabel} className={clsx("inline-flex shrink-0 rounded-sm ring-1 ring-line-2 ring-inset", className)}>
        {options.map((option) => (
          <button
            key={String(option.value)}
            type="button"
            aria-pressed={option.value === shown}
            onClick={() => choose(option.value)}
            className={clsx("h-8 rounded-sm px-2.5 whitespace-nowrap transition-colors touch:h-11", option.value === shown ? "plate" : "text-fg-2 hover:bg-sunk hover:text-fg")}
          >
            {option.label}
          </button>
        ))}
      </div>
      <ErrorText error={error} />
    </>
  );
}
