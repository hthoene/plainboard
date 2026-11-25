/** Areas of a doc site's navigation (#159): the switcher at the top of the tree, the cards on the home page, and
 * which area a page is in. */
import clsx from "clsx";
import { ArrowRight, Check, ChevronsUpDown } from "lucide-react";
import { useRef, useState, type MouseEvent } from "react";
import { useTranslation } from "react-i18next";
import { useDismiss } from "../lib/hooks";
import { areaIcon } from "./areaIcons";
import type { NavArea, NavItem, SiteView } from "./types";

/** The areas that have pages at the top (the others lead nowhere). */
export function shownAreas(site: SiteView, top: NavItem[]): NavArea[] {
  return (site.areas ?? []).filter((a) => top.some((i) => i.area === a.id));
}

/** The area of the page at ``path`` (by its page at the top); none of its own: the first one. */
export function areaOf(areas: NavArea[], top: NavItem[], path: string): NavArea | null {
  if (!areas.length) return null;
  const head = path.split("/")[0];
  const item = top.find((i) => i.path === head) ?? (head ? undefined : top[0]);
  return areas.find((a) => a.id === item?.area) ?? areas[0];
}

/** Where an area starts: its first page at the top. */
export const areaStart = (top: NavItem[], area: NavArea) => top.find((i) => i.area === area.id)?.path ?? "";

interface Props {
  areas: NavArea[];
  hrefOf: (a: NavArea) => string;
  onGo: (href: string) => void;
}

const follow = (e: MouseEvent, href: string, onGo: (href: string) => void) => {
  if (e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
  e.preventDefault();
  onGo(href);
};

/** The area shown, and a menu of all of them with what each holds. */
export function AreaSwitcher({ areas, current, hrefOf, onGo }: Props & { current: NavArea }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useDismiss(ref, open, () => setOpen(false));
  const Icon = areaIcon(current.icon);
  return (
    <div ref={ref} className="doc-switch">
      <button type="button" className="doc-switch-btn" aria-haspopup="menu" aria-expanded={open} aria-label={t("docsSite.areaChoose", { area: current.title })} onClick={() => setOpen((o) => !o)}>
        <span className="doc-switch-icon">
          <Icon className="size-4" />
        </span>
        <span className="min-w-0 flex-1 text-left">
          <span className="doc-switch-title">{current.title}</span>
          {current.description && <span className="doc-switch-desc">{current.description}</span>}
        </span>
        <ChevronsUpDown className="size-4 shrink-0 opacity-60" />
      </button>
      {open && (
        <div className="doc-switch-menu" role="menu" aria-label={t("docsSite.areas")}>
          {areas.map((a) => {
            const href = hrefOf(a);
            const AreaIcon = areaIcon(a.icon);
            const on = a.id === current.id;
            return (
              <a key={a.id} href={href} role="menuitemradio" aria-checked={on} className={clsx("doc-switch-item", on && "is-on")} onClick={(e) => (setOpen(false), follow(e, href, onGo))}>
                <span className="doc-switch-icon">
                  <AreaIcon className="size-4" />
                </span>
                <span className="min-w-0 flex-1">
                  <span className="doc-switch-title">{a.title}</span>
                  {a.description && <span className="doc-switch-desc">{a.description}</span>}
                </span>
                {on && <Check className="size-4 shrink-0" />}
              </a>
            );
          })}
        </div>
      )}
    </div>
  );
}

/** The home page starts with the areas as cards. */
export function AreaCards({ areas, hrefOf, onGo, site, top }: Props & { site: SiteView; top: NavItem[] }) {
  const { t } = useTranslation();
  return (
    <nav className="doc-sections" aria-label={t("docsSite.areas")}>
      {areas.map((a) => {
        const href = hrefOf(a);
        const Icon = areaIcon(a.icon);
        const groups = (site.groups ?? []).filter((g) => g.area === a.id).map((g) => g.title);
        const count = top.filter((i) => i.area === a.id).length;
        return (
          <a key={a.id} href={href} className="doc-section-card" onClick={(e) => follow(e, href, onGo)}>
            <span className="doc-switch-icon is-lg">
              <Icon className="size-5" />
            </span>
            <span className="doc-section-title">
              {a.title}
              <ArrowRight className="size-4 opacity-50" aria-hidden />
            </span>
            {a.description && <span className="doc-switch-desc">{a.description}</span>}
            <span className="doc-section-groups">{[...groups.slice(0, 4), t("docsSite.areaTopics", { count })].join(" · ")}</span>
          </a>
        );
      })}
    </nav>
  );
}
