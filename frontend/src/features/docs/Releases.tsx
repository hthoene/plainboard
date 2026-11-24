import clsx from "clsx";
import { ExternalLink, GitBranch, Pencil, Plus, Tag, Trash2 } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import type { DocRelease, DocSite } from "../../api/types";
import { Badge, Button, ErrorText, Field, FieldRow, IconButton, Input, Modal, Select, Textarea, Toggle } from "../../components/ui";
import { base, releaseHref, releaseOf, useDocsRefresh } from "./queries";

/** A version's date as the list shows it. */
export function releaseDate(iso: string | null): string {
  if (!iso) return "";
  try {
    return new Intl.DateTimeFormat(undefined, { dateStyle: "medium" }).format(new Date(iso));
  } catch {
    return iso.slice(0, 10);
  }
}

/** Its state in a word or two (in development, default, deprecated, not public). */
export function ReleaseBadges({ r, versioned }: { r: DocRelease; versioned: boolean }) {
  const { t } = useTranslation();
  return (
    <>
      {r.working && <Badge tone="amber">{t("docs.releases.inDevelopment")}</Badge>}
      {versioned && r.default && <Badge tone="green">{t("docs.releases.default")}</Badge>}
      {r.deprecated && <Badge>{t("docs.releases.deprecated")}</Badge>}
      {versioned && r.hidden && <Badge>{t("docs.releases.hidden")}</Badge>}
    </>
  );
}

/** Which version the pages, the editor and the translations work on: shown above the tree once a site has versions. */
export function ReleasePicker({ site, release, onRelease, onManage }: { site: DocSite; release: number | null; onRelease: (id: number | null) => void; onManage: () => void }) {
  const { t } = useTranslation();
  const current = releaseOf(site, release);
  const label = (r: DocRelease) =>
    [r.name, r.working ? t("docs.releases.inDevelopment") : null, site.versioned && r.default ? t("docs.releases.default") : null, r.deprecated ? t("docs.releases.deprecated") : null]
      .filter(Boolean)
      .join(" · ");
  return (
    <div className={clsx("shrink-0 space-y-1.5 border-b border-line p-2", current && !current.working && "bg-you-bg")}>
      <div className="flex items-center gap-1.5">
        <GitBranch className={clsx("size-4 shrink-0", current && !current.working ? "text-you-fg" : "text-fg-3")} aria-hidden />
        <Select value={release === null ? "" : String(release)} onChange={(e) => onRelease(e.target.value ? Number(e.target.value) : null)} aria-label={t("docs.releases.editing")} className="min-w-0 flex-1">
          {site.releases.map((r) => (
            <option key={r.id} value={r.working ? "" : String(r.id)}>
              {label(r)}
            </option>
          ))}
        </Select>
        <IconButton label={t("docs.releases.manage")} onClick={onManage}>
          <Tag className="size-4" />
        </IconButton>
      </div>
      {current && !current.working && <p className="px-0.5 text-you-fg">{t("docs.releases.pickerReleased", { name: current.name })}</p>}
    </div>
  );
}

/** The site's versions (#126): whether it uses them at all, cutting one, and per version its name, address, notes,
 * date, whether visitors land on it, whether it is deprecated or hidden. Viewers read. */
export function ReleasesTab({ site, canWrite, onEdit }: { site: DocSite; canWrite: boolean; onEdit: (id: number | null) => void }) {
  const { t } = useTranslation();
  const pid = site.project_id;
  const refresh = useDocsRefresh(pid);
  const [cutting, setCutting] = useState(false);
  const [editing, setEditing] = useState<DocRelease | null>(null);
  const [deleting, setDeleting] = useState<DocRelease | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const released = site.releases.filter((r) => !r.working);
  const working = site.releases.find((r) => r.working);
  const toggle = async (on: boolean) => {
    setBusy(true);
    setError(null);
    try {
      await api.patch(`${base(pid)}/sites/${site.id}`, { versioned: on });
      await refresh();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="scroll-thin min-h-0 flex-1 overflow-y-auto">
      <div className="mx-auto max-w-3xl space-y-5 px-4 py-6 sm:px-8">
        <header className="space-y-2">
          <div className="flex flex-wrap items-center gap-2 rule-b pb-1.5">
            <Tag className="size-4" />
            <h2 className="caps flex-1">{t("docs.releases.title")}</h2>
            {canWrite && (site.versioned || released.length > 0) && (
              <Button size="sm" variant="primary" icon={<Plus className="size-3.5" />} onClick={() => setCutting(true)}>
                {t("docs.releases.cut")}
              </Button>
            )}
          </div>
          <p className="text-fg-2">{t("docs.releases.hint")}</p>
        </header>

        <div className="space-y-1.5">
          {canWrite ? (
            <div className={clsx(busy && "pointer-events-none opacity-60")}>
              <Toggle checked={site.versioned} onChange={toggle} label={<span className="font-semibold">{t("docs.releases.use")}</span>} />
            </div>
          ) : (
            <p className="font-semibold">{t(site.versioned ? "docs.releases.usedYes" : "docs.releases.usedNo")}</p>
          )}
          <p className="text-fg-3">{t(site.versioned ? "docs.releases.onHint" : released.length ? "docs.releases.offKeptHint" : "docs.releases.offHint")}</p>
          <ErrorText error={error} />
        </div>

        {!site.versioned && released.length === 0 ? (
          <div className="flex flex-col items-center gap-3 rounded-sm px-6 py-10 text-center ring-1 ring-line ring-inset">
            <GitBranch className="size-8 text-fg-3" />
            <p className="max-w-md text-fg-2">{t("docs.releases.firstHint")}</p>
            {canWrite && (
              <Button variant="primary" icon={<Plus className="size-4" />} onClick={() => setCutting(true)}>
                {t("docs.releases.cutFirst")}
              </Button>
            )}
          </div>
        ) : (
          <ul className="border-t border-line">
            {[...(working ? [working] : []), ...released].map((r) => (
              <li key={r.id} className="flex items-start gap-3 border-b border-line py-3">
                <span className={clsx("mt-0.5 shrink-0", r.working ? "text-you-fg" : "text-fg-3")}>{r.working ? <GitBranch className="size-4" /> : <Tag className="size-4" />}</span>
                <div className="min-w-0 flex-1 space-y-1">
                  <div className="flex flex-wrap items-center gap-1.5">
                    <span className="font-semibold">{r.name}</span>
                    <ReleaseBadges r={r} versioned={site.versioned} />
                  </div>
                  <p className="flex flex-wrap gap-x-3 gap-y-0.5 text-fg-3">
                    <span>{r.working ? t("docs.releases.workingHint") : t("docs.releases.releasedOn", { date: releaseDate(r.released_on) })}</span>
                    {site.versioned && !r.hidden && site.published && (
                      <a href={releaseHref(site, r)} target="_blank" rel="noreferrer" className="inline-flex min-w-0 items-center gap-1 hover:text-fg hover:underline">
                        <span className="truncate">{releaseHref(site, r).replace(/^https?:\/\//, "")}</span>
                        <ExternalLink className="size-3 shrink-0" />
                      </a>
                    )}
                  </p>
                  {r.notes && <p className="line-clamp-3 whitespace-pre-line text-fg-2">{r.notes}</p>}
                  <div className="flex flex-wrap gap-1.5 pt-1">
                    <Button size="sm" icon={<Pencil className="size-3.5" />} onClick={() => onEdit(r.working ? null : r.id)}>
                      {t(canWrite ? "docs.releases.editPages" : "docs.releases.readPages")}
                    </Button>
                    {canWrite && (
                      <Button size="sm" variant="ghost" onClick={() => setEditing(r)}>
                        {t("docs.releases.settings")}
                      </Button>
                    )}
                  </div>
                </div>
                {canWrite && !r.working && (
                  <IconButton label={t("docs.releases.delete", { name: r.name })} onClick={() => setDeleting(r)}>
                    <Trash2 className="size-4" />
                  </IconButton>
                )}
              </li>
            ))}
          </ul>
        )}
      </div>
      {cutting && working && <CutDialog site={site} working={working} first={released.length === 0} onClose={() => setCutting(false)} />}
      {editing && <EditDialog site={site} release={editing} onClose={() => setEditing(null)} />}
      {deleting && <DeleteDialog site={site} release={deleting} onClose={() => setDeleting(null)} onDeleted={() => onEdit(null)} />}
    </div>
  );
}

const today = () => new Date().toISOString().slice(0, 10);
const slugOf = (name: string) =>
  name
    .trim()
    .toLowerCase()
    .replace(/[^a-z0-9._-]+/g, "-")
    .replace(/^[-._]+|[-._]+$/g, "")
    .slice(0, 40);

function CutDialog({ site, working, first, onClose }: { site: DocSite; working: DocRelease; first: boolean; onClose: () => void }) {
  const { t } = useTranslation();
  const refresh = useDocsRefresh(site.project_id);
  const [name, setName] = useState("");
  const [slug, setSlug] = useState("");
  const [notes, setNotes] = useState("");
  const [date, setDate] = useState(today());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const cut = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.post(`${base(site.project_id)}/sites/${site.id}/releases`, {
        name: name.trim(),
        ...(slug.trim() ? { slug: slug.trim() } : {}),
        notes,
        released_on: date ? `${date}T12:00:00Z` : null,
      });
      await refresh();
      onClose();
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };
  return (
    <Modal
      open
      onClose={onClose}
      width="max-w-lg"
      title={t(first ? "docs.releases.cutFirst" : "docs.releases.cut")}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button variant="primary" loading={busy} disabled={!name.trim()} onClick={cut}>
            {t("docs.releases.cutNow", { name: name.trim() || "…" })}
          </Button>
        </>
      }
    >
      <form
        className="space-y-4"
        onSubmit={(e) => {
          e.preventDefault();
          if (name.trim()) void cut();
        }}
      >
        <p className="text-fg-2">{t("docs.releases.cutHint", { working: working.name })}</p>
        {first && !site.versioned && <p className="rounded-sm bg-sunk px-3 py-2 text-fg-2">{t("docs.releases.cutTurnsOn")}</p>}
        <FieldRow>
          <Field label={t("docs.releases.name")} hint={t("docs.releases.nameHint")}>
            <Input autoFocus value={name} maxLength={80} onChange={(e) => setName(e.target.value)} placeholder="1.0" />
          </Field>
          <Field label={t("docs.releases.slug")} hint={t("docs.releases.slugHint", { url: `${site.url}${slug || slugOf(name) || "1.0"}/` })}>
            <Input value={slug} maxLength={40} onChange={(e) => setSlug(e.target.value.toLowerCase().replace(/[^a-z0-9._-]/g, "-"))} placeholder={slugOf(name) || "1.0"} />
          </Field>
        </FieldRow>
        <Field label={t("docs.releases.date")}>
          <Input type="date" value={date} onChange={(e) => setDate(e.target.value)} className="w-auto" />
        </Field>
        <Field label={t("docs.releases.notes")} hint={t("docs.releases.notesHint")}>
          <Textarea rows={4} value={notes} maxLength={20000} onChange={(e) => setNotes(e.target.value)} />
        </Field>
        <ErrorText error={error} />
      </form>
    </Modal>
  );
}

function EditDialog({ site, release, onClose }: { site: DocSite; release: DocRelease; onClose: () => void }) {
  const { t } = useTranslation();
  const refresh = useDocsRefresh(site.project_id);
  const [name, setName] = useState(release.name);
  const [slug, setSlug] = useState(release.slug);
  const [notes, setNotes] = useState(release.notes);
  const [date, setDate] = useState(release.released_on?.slice(0, 10) ?? "");
  const [isDefault, setDefault] = useState(release.is_default);
  const [deprecated, setDeprecated] = useState(release.deprecated);
  const [hidden, setHidden] = useState(release.hidden);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const change: Record<string, unknown> = {};
  if (name.trim() !== release.name) change.name = name.trim();
  if (slug !== release.slug) change.slug = slug;
  if (notes !== release.notes) change.notes = notes;
  if (!release.working && date && date !== release.released_on?.slice(0, 10)) change.released_on = `${date}T12:00:00Z`;
  if (isDefault !== release.is_default) change.default = isDefault;
  if (deprecated !== release.deprecated) change.deprecated = deprecated;
  if (hidden !== release.hidden) change.hidden = hidden;
  const save = async () => {
    setBusy(true);
    setError(null);
    try {
      await api.patch(`${base(site.project_id)}/releases/${release.id}`, change);
      await refresh();
      onClose();
    } catch (e) {
      setError(e);
      setBusy(false);
    }
  };
  return (
    <Modal
      open
      onClose={onClose}
      width="max-w-lg"
      title={t("docs.releases.settingsOf", { name: release.name })}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button variant="primary" loading={busy} disabled={!name.trim() || !slug || Object.keys(change).length === 0} onClick={save}>
            {t("common.save")}
          </Button>
        </>
      }
    >
      <div className="space-y-4">
        <FieldRow>
          <Field label={t("docs.releases.name")}>
            <Input value={name} maxLength={80} onChange={(e) => setName(e.target.value)} />
          </Field>
          <Field label={t("docs.releases.slug")} hint={t("docs.releases.slugHint", { url: `${site.url}${slug}/` })}>
            <Input value={slug} maxLength={40} onChange={(e) => setSlug(e.target.value.toLowerCase().replace(/[^a-z0-9._-]/g, "-"))} />
          </Field>
        </FieldRow>
        {!release.working && (
          <Field label={t("docs.releases.date")}>
            <Input type="date" value={date} onChange={(e) => setDate(e.target.value)} className="w-auto" />
          </Field>
        )}
        <Field label={t("docs.releases.notes")} hint={t("docs.releases.notesHint")}>
          <Textarea rows={4} value={notes} maxLength={20000} onChange={(e) => setNotes(e.target.value)} />
        </Field>
        <div className="space-y-3 border-t border-line pt-4">
          <Choice checked={isDefault} onChange={setDefault} label={t("docs.releases.makeDefault")} hint={t("docs.releases.makeDefaultHint")} />
          {!release.working && <Choice checked={deprecated} onChange={setDeprecated} label={t("docs.releases.markDeprecated")} hint={t("docs.releases.markDeprecatedHint")} />}
          <Choice
            checked={hidden}
            onChange={setHidden}
            label={t(release.working ? "docs.releases.hideWorking" : "docs.releases.hide")}
            hint={t(release.working ? "docs.releases.hideWorkingHint" : "docs.releases.hideHint")}
          />
        </div>
        <ErrorText error={error} />
      </div>
    </Modal>
  );
}

function Choice({ checked, onChange, label, hint }: { checked: boolean; onChange: (v: boolean) => void; label: string; hint: string }) {
  return (
    <div className="space-y-1">
      <Toggle checked={checked} onChange={onChange} label={label} />
      <p className="pl-11 text-fg-3">{hint}</p>
    </div>
  );
}

function DeleteDialog({ site, release, onClose, onDeleted }: { site: DocSite; release: DocRelease; onClose: () => void; onDeleted: () => void }) {
  const { t } = useTranslation();
  const refresh = useDocsRefresh(site.project_id);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  return (
    <Modal
      open
      onClose={onClose}
      width="max-w-md"
      title={t("docs.releases.delete", { name: release.name })}
      footer={
        <>
          <Button variant="ghost" onClick={onClose}>
            {t("common.cancel")}
          </Button>
          <Button
            variant="danger"
            loading={busy}
            onClick={async () => {
              setBusy(true);
              setError(null);
              try {
                await api.del(`${base(site.project_id)}/releases/${release.id}`);
                onDeleted();
                await refresh();
                onClose();
              } catch (e) {
                setError(e);
                setBusy(false);
              }
            }}
          >
            {t("docs.releases.deleteSure")}
          </Button>
        </>
      }
    >
      <p className="text-fg-2">{t("docs.releases.deleteHint", { name: release.name })}</p>
      <ErrorText error={error} />
    </Modal>
  );
}
