import { ArrowDown, ArrowUp, FolderTree, Heading, Pencil, Plus, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import type { DocArea, DocGroup, DocSite } from "../../api/types";
import { Button, ErrorText, Field, IconButton, Input, Modal, Select, Spinner } from "../../components/ui";
import { AREA_ICONS, areaIcon } from "../../docs/areaIcons";
import { languageLabel } from "./PageEditor";
import { base, rel, useDocOutline, useDocsRefresh, useRelease } from "./queries";

type Editing = { kind: "area"; row: DocArea | null } | { kind: "group"; row: DocGroup | null; area: number | null };

/** A doc site's areas and groups (#159): the areas a switcher at the top of the served navigation, the groups headings
 * over the pages at the top. Here in their order, each area with its groups; pages are put in them in their settings. */
export function Outline({ pid, site }: { pid: number; site: DocSite }) {
  const { t } = useTranslation();
  const outline = useDocOutline(pid, site.id);
  const release = useRelease();
  const refresh = useDocsRefresh(pid);
  const [editing, setEditing] = useState<Editing | null>(null);
  const [error, setError] = useState<unknown>(null);
  const areas = outline.data?.areas ?? [];
  const groups = outline.data?.groups ?? [];
  const url = (path: string) => rel(`${base(pid)}/sites/${site.id}/${path}`, release);

  const run = async (call: Promise<unknown>) => {
    setError(null);
    try {
      await call;
    } catch (e) {
      setError(e);
    }
    await refresh();
  };
  /** Moves a group before or after its neighbour in the same list (positions count over all groups). */
  const moveGroup = (list: DocGroup[], i: number, by: -1 | 1) => run(api.patch(url(`groups/${list[i].id}`), { position: groups.indexOf(list[i + by]) }));
  const groupList = (area: number | null) => {
    const list = groups.filter((g) => g.area_id === area);
    if (!list.length) return null;
    return (
      <ol className="divide-y divide-line">
        {list.map((g, i) => (
          <li key={g.id} className="flex items-center gap-1 py-0.5 pr-1 pl-9">
            <Heading className="size-3.5 shrink-0 text-fg-3" aria-hidden />
            <span className="min-w-0 flex-1 truncate py-1.5 pl-1.5 text-fg-2">{g.title}</span>
            <IconButton label={t("docs.outline.up")} disabled={i === 0} onClick={() => moveGroup(list, i, -1)}>
              <ArrowUp className="size-4" />
            </IconButton>
            <IconButton label={t("docs.outline.down")} disabled={i === list.length - 1} onClick={() => moveGroup(list, i, 1)}>
              <ArrowDown className="size-4" />
            </IconButton>
            <IconButton label={t("docs.outline.edit")} onClick={() => setEditing({ kind: "group", row: g, area: g.area_id })}>
              <Pencil className="size-4" />
            </IconButton>
          </li>
        ))}
      </ol>
    );
  };

  return (
    <div className="space-y-4">
      {outline.isLoading ? (
        <Spinner />
      ) : areas.length + groups.length === 0 ? (
        <div className="rounded-sm p-4 text-fg-2 ring-1 ring-line ring-inset">{t("docs.outline.empty")}</div>
      ) : (
        <ol data-part="doc-outline" className="divide-y divide-line rounded-sm ring-1 ring-line ring-inset">
          {areas.map((a, i) => {
            const Icon = areaIcon(a.icon);
            return (
              <li key={a.id}>
                <div className="flex items-center gap-1 py-1 pr-1 pl-3">
                  <Icon className="size-4 shrink-0 text-fg-3" aria-hidden />
                  <span className="min-w-0 flex-1 py-1 pl-1.5">
                    <span className="block truncate font-medium">{a.title}</span>
                    {a.description && <span className="block truncate text-fg-3">{a.description}</span>}
                  </span>
                  <IconButton label={t("docs.outline.addGroup")} onClick={() => setEditing({ kind: "group", row: null, area: a.id })}>
                    <Plus className="size-4" />
                  </IconButton>
                  <IconButton label={t("docs.outline.up")} disabled={i === 0} onClick={() => run(api.patch(url(`areas/${a.id}`), { position: i - 1 }))}>
                    <ArrowUp className="size-4" />
                  </IconButton>
                  <IconButton label={t("docs.outline.down")} disabled={i === areas.length - 1} onClick={() => run(api.patch(url(`areas/${a.id}`), { position: i + 1 }))}>
                    <ArrowDown className="size-4" />
                  </IconButton>
                  <IconButton label={t("docs.outline.edit")} onClick={() => setEditing({ kind: "area", row: a })}>
                    <Pencil className="size-4" />
                  </IconButton>
                </div>
                {groupList(a.id)}
              </li>
            );
          })}
          {groups.some((g) => g.area_id === null) && (
            <li>
              <div className="flex items-center gap-2.5 py-2 pl-3 text-fg-3">
                <FolderTree className="size-4 shrink-0" aria-hidden />
                {areas.length ? t("docs.outline.everyArea") : t("docs.outline.groups")}
              </div>
              {groupList(null)}
            </li>
          )}
        </ol>
      )}
      <div className="flex flex-wrap gap-2">
        <Button icon={<Plus className="size-4" />} onClick={() => setEditing({ kind: "area", row: null })}>
          {t("docs.outline.addArea")}
        </Button>
        <Button icon={<Plus className="size-4" />} onClick={() => setEditing({ kind: "group", row: null, area: null })}>
          {t("docs.outline.addGroup")}
        </Button>
      </div>
      <ErrorText error={error} />
      <p className="text-fg-3">{t("docs.outline.rules")}</p>
      {editing && (
        <OutlineEditor
          key={`${editing.kind}-${editing.row?.id ?? "new"}`}
          site={site}
          editing={editing}
          areas={areas}
          onClose={() => setEditing(null)}
          onSave={async (body) => {
            const path = `${editing.kind}s`;
            if (editing.row) await api.patch(url(`${path}/${editing.row.id}`), body);
            else await api.post(url(path), body);
            await refresh();
            setEditing(null);
          }}
          onDelete={
            editing.row
              ? async () => {
                  await api.del(url(`${editing.kind}s/${editing.row!.id}`));
                  await refresh();
                  setEditing(null);
                }
              : undefined
          }
        />
      )}
    </div>
  );
}

type Texts = Record<string, { title?: string; description?: string }>;

function OutlineEditor({
  site,
  editing,
  areas,
  onClose,
  onSave,
  onDelete,
}: {
  site: DocSite;
  editing: Editing;
  areas: DocArea[];
  onClose: () => void;
  onSave: (body: Record<string, unknown>) => Promise<void>;
  onDelete?: () => Promise<void>;
}) {
  const { t } = useTranslation();
  const isArea = editing.kind === "area";
  const row = editing.row;
  const [title, setTitle] = useState(row?.title ?? "");
  const [description, setDescription] = useState(editing.kind === "area" ? (editing.row?.description ?? "") : "");
  const [icon, setIcon] = useState(editing.kind === "area" ? editing.row?.icon || "book-open" : "");
  const [area, setArea] = useState(editing.kind === "group" ? editing.area : null);
  const [texts, setTexts] = useState<Texts>((row?.texts as Texts | undefined) ?? {});
  const [busy, setBusy] = useState<"save" | "delete" | null>(null);
  const [error, setError] = useState<unknown>(null);
  const act = async (which: "save" | "delete", call: () => Promise<void>) => {
    setBusy(which);
    setError(null);
    try {
      await call();
    } catch (e) {
      setError(e);
      setBusy(null);
    }
  };
  const save = () => act("save", () => onSave(isArea ? { title: title.trim(), description: description.trim(), icon, texts } : { title: title.trim(), area_id: area, texts }));
  return (
    <Modal
      open
      onClose={onClose}
      width="max-w-lg"
      title={t(row ? (isArea ? "docs.outline.editArea" : "docs.outline.editGroup") : isArea ? "docs.outline.newArea" : "docs.outline.newGroup")}
      footer={
        <>
          {onDelete && (
            <Button variant="danger" icon={<Trash2 className="size-4" />} loading={busy === "delete"} className="mr-auto" onClick={() => act("delete", onDelete)}>
              {t("common.delete")}
            </Button>
          )}
          <Button variant="ghost" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button variant="primary" disabled={!title.trim()} loading={busy === "save"} onClick={save}>
            {t("common.save")}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <Field label={t("docs.outline.title")}>
          <Input value={title} maxLength={100} autoFocus onChange={(e) => setTitle(e.target.value)} onKeyDown={(e) => e.key === "Enter" && title.trim() && save()} />
        </Field>
        {isArea ? (
          <>
            <Field label={t("docs.outline.description")} hint={t("docs.outline.descriptionHint")}>
              <Input value={description} maxLength={200} onChange={(e) => setDescription(e.target.value)} />
            </Field>
            <Field label={t("docs.outline.icon")} group>
              <Select value={icon} onChange={(e) => setIcon(e.target.value)} aria-label={t("docs.outline.icon")}>
                {Object.keys(AREA_ICONS).map((name) => {
                  const Option = AREA_ICONS[name];
                  return (
                    <option key={name} value={name}>
                      <span className="inline-flex items-center gap-2">
                        <Option className="size-4" aria-hidden />
                        {name}
                      </span>
                    </option>
                  );
                })}
              </Select>
            </Field>
          </>
        ) : (
          <Field label={t("docs.outline.area")} hint={t("docs.outline.areaHint")} group>
            <Select value={area ?? ""} onChange={(e) => setArea(e.target.value ? Number(e.target.value) : null)} aria-label={t("docs.outline.area")}>
              <option value="">{t("docs.outline.noArea")}</option>
              {areas.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.title}
                </option>
              ))}
            </Select>
          </Field>
        )}
        {site.languages.map((lang) => (
          <div key={lang} className="space-y-2">
            <Field label={t("docs.outline.titleIn", { language: languageLabel(lang) })}>
              <Input value={texts[lang]?.title ?? ""} placeholder={title} maxLength={100} onChange={(e) => setTexts((old) => ({ ...old, [lang]: { ...old[lang], title: e.target.value } }))} />
            </Field>
            {isArea && (
              <Field label={t("docs.outline.descriptionIn", { language: languageLabel(lang) })}>
                <Input
                  value={texts[lang]?.description ?? ""}
                  placeholder={description}
                  maxLength={200}
                  onChange={(e) => setTexts((old) => ({ ...old, [lang]: { ...old[lang], description: e.target.value } }))}
                />
              </Field>
            )}
          </div>
        ))}
        {onDelete && <p className="text-fg-3">{t(isArea ? "docs.outline.deleteAreaHint" : "docs.outline.deleteGroupHint")}</p>}
        <ErrorText error={error} />
      </div>
    </Modal>
  );
}
