import { useEffect, useLayoutEffect, useRef, useState, useSyncExternalStore, type CSSProperties, type RefObject } from "react";

/** State persisted in localStorage (JSON). */
export function useStored<T>(key: string, initial: T) {
  const [value, setValue] = useState<T>(() => {
    const raw = localStorage.getItem(key);
    if (raw === null) return initial;
    try {
      return JSON.parse(raw) as T;
    } catch {
      return initial;
    }
  });
  useEffect(() => localStorage.setItem(key, JSON.stringify(value)), [key, value]);
  return [value, setValue] as const;
}

const storedListeners = new Set<() => void>();

/** Like useStored, but every component that reads the key sees a change at once (a setting that one
 * place switches and another one uses). The value is JSON in localStorage, as with useStored. */
export function useSharedStored<T>(key: string, initial: T) {
  const raw = useSyncExternalStore(
    (onChange) => {
      storedListeners.add(onChange);
      window.addEventListener("storage", onChange);
      return () => {
        storedListeners.delete(onChange);
        window.removeEventListener("storage", onChange);
      };
    },
    () => localStorage.getItem(key),
  );
  let value = initial;
  if (raw !== null) {
    try {
      value = JSON.parse(raw) as T;
    } catch {
      /* keep the default */
    }
  }
  const set = (next: T) => {
    localStorage.setItem(key, JSON.stringify(next));
    storedListeners.forEach((notify) => notify());
  };
  return [value, set] as const;
}

export function useMediaQuery(query: string): boolean {
  return useSyncExternalStore(
    (onChange) => {
      const mql = window.matchMedia(query);
      mql.addEventListener("change", onChange);
      return () => mql.removeEventListener("change", onChange);
    },
    () => window.matchMedia(query).matches,
  );
}

/** Desktop layout (sidebar + side chat panel) from Tailwind's `lg` breakpoint. */
export const useIsDesktop = () => useMediaQuery("(min-width: 1024px)");

/** Closes an open menu or popover on a click or tap outside `ref` and on Escape. Capture phase, so it
 * also works inside modals that stop events from bubbling. */
export function useDismiss(ref: RefObject<HTMLElement | null>, open: boolean, close: () => void) {
  const onClose = useRef(close);
  onClose.current = close;
  useEffect(() => {
    if (!open) return;
    const away = (e: PointerEvent) => !ref.current?.contains(e.target as Node) && onClose.current();
    const onKey = (e: KeyboardEvent) => e.key === "Escape" && onClose.current();
    document.addEventListener("pointerdown", away, true);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("pointerdown", away, true);
      document.removeEventListener("keydown", onKey);
    };
  }, [ref, open]);
}

/** Keeps an open popover half a rem inside the screen: give it the returned ref and style. If it sticks
 * out left or right (on a phone its anchor may sit anywhere in the bar), it moves sideways by that much. */
export function useInView<T extends HTMLElement>(open: boolean) {
  const ref = useRef<T>(null);
  const [shift, setShift] = useState(0);
  useLayoutEffect(() => {
    if (!open || !ref.current) return setShift(0);
    const r = ref.current.getBoundingClientRect();
    // Not innerWidth: on a phone it grows with the very popover that sticks out.
    const [edge, width] = [8, document.documentElement.clientWidth];
    setShift(r.right > width - edge ? Math.max(edge - r.left, width - edge - r.right) : r.left < edge ? edge - r.left : 0);
  }, [open]);
  const style: CSSProperties | undefined = shift ? { transform: `translateX(${shift}px)` } : undefined;
  return [ref, style] as const;
}

/** A textarea that grows with its text (#134): from its `rows` (or CSS min-height) up to its CSS
 * max-height, then it scrolls. Measures again when the text or the field's width changes. Scroll
 * positions of the page and the scrolling boxes around it stay put while it measures. */
export function useAutoGrow(ref: RefObject<HTMLTextAreaElement | null>, value: unknown) {
  const fit = useRef<() => void>(() => undefined);
  fit.current = () => {
    const el = ref.current;
    if (!el) return;
    const kept: [Element, number][] = [];
    for (let p = el.parentElement; p; p = p.parentElement) if (p.scrollTop) kept.push([p, p.scrollTop]);
    const page = document.scrollingElement;
    if (page?.scrollTop) kept.push([page, page.scrollTop]);
    el.style.height = "auto";
    const border = el.offsetHeight - el.clientHeight;
    el.style.height = `${el.scrollHeight + border}px`;
    // No scrollbar while the text fits (#169): phones round line heights so that a single line can overflow by a pixel.
    el.style.overflowY = el.scrollHeight > el.clientHeight + 1 ? "auto" : "hidden";
    for (const [box, top] of kept) box.scrollTop = top;
  };
  useLayoutEffect(() => fit.current(), [value]);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof ResizeObserver === "undefined") return;
    let width = el.clientWidth;
    const watch = new ResizeObserver(() => {
      if (el.clientWidth === width) return;
      width = el.clientWidth;
      fit.current();
    });
    watch.observe(el);
    return () => watch.disconnect();
  }, [ref]);
}

/** Whether this key press sends (#169): Enter sends and Shift+Enter is a new line; on touch screens Enter is a
 * new line and the send button sends. */
export function sendsOnEnter(e: { key: string; shiftKey: boolean; nativeEvent: { isComposing: boolean } }): boolean {
  return e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing && !window.matchMedia("(pointer: coarse)").matches;
}

/** The value once it has not changed for ``ms``. */
export function useDebounced<T>(value: T, ms: number): T {
  const [out, setOut] = useState(value);
  useEffect(() => {
    const id = setTimeout(() => setOut(value), ms);
    return () => clearTimeout(id);
  }, [value, ms]);
  return out;
}

/** A value that a control saves at once, shown changed before the server answers (#188): `[shown, change]`.
 * `change(next, save)` calls `save(next)`; while the promise it returns runs, `next` is shown, after it the
 * value again (the saved one, or the old one when it failed). A failure is passed on to the caller. A save that
 * returns no promise changes local state, which shows at once anyway. */
export function useOptimistic<T>(value: T) {
  const [pending, setPending] = useState<{ value: T } | null>(null);
  const last = useRef(0);
  const change = async (next: T, save: (next: T) => unknown) => {
    const result = save(next);
    if (!(result instanceof Promise)) return;
    const mine = ++last.current;
    setPending({ value: next });
    try {
      await result;
    } finally {
      if (last.current === mine) setPending(null);
    }
  };
  return [pending ? pending.value : value, change] as const;
}
