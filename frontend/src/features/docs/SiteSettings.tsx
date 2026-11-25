import clsx from "clsx";
import { Check, Cloud, Copy, ExternalLink, Globe2, KeyRound, Loader2, Plus, RefreshCw, ShieldAlert, Trash2, X } from "lucide-react";
import { useState, type ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { api, errorMessage } from "../../api/client";
import { useMeta } from "../../api/queries";
import type { DocSite, DocTranslator } from "../../api/types";
import { SaveRow, SettingsLayout, type SettingsSection } from "../../components/SettingsLayout";
import { Badge, Button, ErrorText, Field, FieldRow, Input, Modal, Segmented, Select, Toggle } from "../../components/ui";
import { formatDateTime } from "../../lib/format";
import { ModelSelect } from "../team/AgentEditor";
import { CustomPages } from "./CustomPages";
import { DocApi } from "./DocApi";
import { Outline } from "./Outline";
import { languageLabel } from "./PageEditor";
import { base, useDocDomain, useDocsRefresh } from "./queries";
import { SiteLook } from "./SiteLook";

/** Further languages offered first; any other code can be typed. */
const COMMON = ["de", "fr", "es", "it", "pt", "nl", "pl", "sv", "da", "fi", "nb", "cs", "tr", "uk", "ru", "ja", "zh", "ko", "ar", "hi"];

export function SiteSettings({ pid, site, isOwner, section, onSection, onDeleted }: { pid: number; site: DocSite; isOwner: boolean; section: string; onSection: (s: string) => void; onDeleted: () => void }) {
  const { t } = useTranslation();
  const sections: SettingsSection[] = [
    { id: "general", label: t("docs.settings.general"), description: t("docs.settings.generalHint"), content: <General pid={pid} site={site} /> },
    { id: "look", label: t("docs.look.section"), description: t("docs.look.sectionHint"), content: <SiteLook pid={pid} site={site} /> },
    { id: "navigation", label: t("docs.outline.section"), description: t("docs.outline.sectionHint"), content: <Outline pid={pid} site={site} /> },
    { id: "footer", label: t("docs.custom.section"), description: t("docs.custom.sectionHint"), content: <CustomPages pid={pid} site={site} /> },
    { id: "languages", label: t("docs.settings.languages"), description: t("docs.settings.languagesHint"), content: <Languages pid={pid} site={site} isOwner={isOwner} /> },
    { id: "agent", label: t("docs.settings.agent"), description: t("docs.settings.agentHint"), content: <DocAgent pid={pid} site={site} /> },
    { id: "assistant", label: t("docs.assistant.section"), description: t("docs.assistant.sectionHint"), content: <AssistantSettings pid={pid} site={site} /> },
    { id: "api", label: t("docs.api.section"), description: t("docs.api.sectionHint"), content: <DocApi pid={pid} site={site} /> },
    { id: "domain", label: t("docs.settings.domain"), description: t("docs.settings.domainHint"), content: <Domain pid={pid} site={site} isOwner={isOwner} /> },
    { id: "delete", label: t("docs.settings.delete"), description: t("docs.settings.deleteHint"), content: <Delete pid={pid} site={site} onDeleted={onDeleted} /> },
  ];
  return <SettingsLayout sections={sections} current={section} onChange={onSection} label={t("docs.tabs.settings")} />;
}

function usePatch(pid: number, site: DocSite) {
  const refresh = useDocsRefresh(pid);
  return async (body: Record<string, unknown>) => {
    await api.patch(`${base(pid)}/sites/${site.id}`, body);
    await refresh();
  };
}

// --- general --------------------------------------------------------------------------------------------------

function General({ pid, site }: { pid: number; site: DocSite }) {
  const { t } = useTranslation();
  const patch = usePatch(pid, site);
  const [name, setName] = useState(site.name);
  const [slug, setSlug] = useState(site.slug);
  const [description, setDescription] = useState(site.description);
  const [published, setPublished] = useState(site.published);
  const [isPublic, setPublic] = useState(site.public);
  const dirty = name !== site.name || slug !== site.slug || description !== site.description || published !== site.published || isPublic !== site.public;
  return (
    <div className="space-y-4">
      <FieldRow>
        <Field label={t("docs.settings.name")} hint={t("docs.settings.nameHint")}>
          <Input value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label={t("docs.settings.slug")} hint={t("docs.settings.slugHint", { url: `/docs/${slug}/` })}>
          <Input value={slug} onChange={(e) => setSlug(e.target.value.toLowerCase().replace(/[^a-z0-9-]/g, "-"))} />
        </Field>
      </FieldRow>
      <Field label={t("docs.settings.description")} hint={t("docs.settings.descriptionHint")}>
        <Input value={description} maxLength={300} onChange={(e) => setDescription(e.target.value)} />
      </Field>
      <Visibility published={published} isPublic={isPublic} onPublished={setPublished} onPublic={setPublic} />
      <SaveRow dirty={dirty} onSave={() => patch({ name, slug, description, published, public: isPublic })} />
    </div>
  );
}

export function Visibility({ published, isPublic, onPublished, onPublic }: { published: boolean; isPublic: boolean; onPublished: (v: boolean) => void; onPublic: (v: boolean) => void }) {
  const { t } = useTranslation();
  return (
    <Field label={t("docs.settings.visibility")} group hint={t(!published ? "docs.visibility.draftHint" : isPublic ? "docs.visibility.publicHint" : "docs.visibility.privateHint")}>
      <Segmented
        value={!published ? "draft" : isPublic ? "public" : "private"}
        onChange={(v) => {
          onPublished(v !== "draft");
          if (v !== "draft") onPublic(v === "public");
        }}
        aria-label={t("docs.settings.visibility")}
        options={[
          { value: "draft", label: t("docs.visibility.draft") },
          { value: "private", label: t("docs.visibility.private") },
          { value: "public", label: t("docs.visibility.public") },
        ]}
      />
    </Field>
  );
}

// --- languages ------------------------------------------------------------------------------------------------

function Languages({ pid, site, isOwner }: { pid: number; site: DocSite; isOwner: boolean }) {
  const { t } = useTranslation();
  const meta = useMeta();
  const patch = usePatch(pid, site);
  const [languages, setLanguages] = useState<string[]>(site.languages);
  const [translator, setTranslator] = useState<DocTranslator>(site.translator || "");
  const [provider, setProvider] = useState(site.translate_provider);
  const [model, setModel] = useState(site.translate_model);
  const [code, setCode] = useState("");
  const [asked, setAsked] = useState(false);
  const providers = meta.data?.providers ?? [];
  const providerInfo = providers.find((p) => p.name === (provider || providers[0]?.name));
  const dirty =
    languages.join(",") !== site.languages.join(",") || translator !== (site.translator || "") || provider !== site.translate_provider || model !== site.translate_model;
  const add = (c: string) => {
    const clean = c.trim().toLowerCase();
    if (/^[a-z]{2,3}(-[a-z0-9]{2,8})?$/.test(clean) && clean !== site.language && !languages.includes(clean)) setLanguages([...languages, clean]);
    setCode("");
  };
  return (
    <div className="space-y-5">
      <Field label={t("docs.settings.ownLanguage")} hint={t("docs.settings.ownLanguageHint")} group>
        <p className="font-medium">
          {languageLabel(site.language)} <span className="text-fg-3 uppercase">{site.language}</span>
        </p>
      </Field>
      <Field label={t("docs.settings.further")} hint={t("docs.settings.furtherHint")} group>
        <div className="flex flex-wrap gap-1.5">
          {languages.map((l) => (
            <span key={l} data-part="tag" className="inline-flex h-7 items-center gap-1 rounded-sm pl-2 ring-1 ring-line-2 ring-inset touch:h-9">
              {languageLabel(l)} <span className="text-fg-3 uppercase">{l}</span>
              <button type="button" onClick={() => setLanguages(languages.filter((x) => x !== l))} aria-label={t("common.remove")} className="inline-flex size-6 items-center justify-center text-fg-3 hover:text-fg">
                <X className="size-3.5" />
              </button>
            </span>
          ))}
          {languages.length === 0 && <span className="text-fg-3">{t("docs.settings.noFurther")}</span>}
        </div>
        <div className="flex flex-wrap gap-2">
          <Select value="" onChange={(e) => e.target.value && add(e.target.value)} className="w-56" aria-label={t("docs.settings.addLanguage")}>
            <option value="">{t("docs.settings.addLanguage")}</option>
            {COMMON.filter((c) => c !== site.language && !languages.includes(c)).map((c) => (
              <option key={c} value={c}>
                {`${languageLabel(c)} (${c})`}
              </option>
            ))}
          </Select>
          <div className="flex gap-1">
            <Input value={code} onChange={(e) => setCode(e.target.value)} onKeyDown={(e) => e.key === "Enter" && add(code)} placeholder="pt-br" className="w-28" aria-label={t("docs.settings.code")} />
            <Button icon={<Plus className="size-4" />} onClick={() => add(code)} disabled={!code.trim()} aria-label={t("docs.settings.addCode")} />
          </div>
        </div>
      </Field>
      <Field label={t("docs.settings.translator")} hint={t(`docs.translator.${translator || "none"}Hint`)} group>
        <Segmented
          value={translator}
          onChange={setTranslator}
          aria-label={t("docs.settings.translator")}
          options={[
            { value: "", label: t("docs.translator.none") },
            { value: "llm", label: t("docs.translator.llm") },
            { value: "deepl", label: "DeepL" },
            { value: "google", label: "Google" },
          ]}
        />
      </Field>
      {translator === "llm" && (
        <FieldRow>
          <Field label={t("team.provider")} hint={t("docs.settings.translateProviderHint")}>
            <Select value={provider} onChange={(e) => (setProvider(e.target.value), setModel(""))}>
              <option value="">{t("docs.settings.projectDefault")}</option>
              {providers.map((p) => (
                <option key={p.name} value={p.name}>
                  {p.label}
                </option>
              ))}
            </Select>
          </Field>
          <Field label={t("team.model")} hint={t("docs.settings.translateModelHint")}>
            <ModelSelect provider={providerInfo} value={model} onChange={setModel} />
          </Field>
        </FieldRow>
      )}
      <SaveRow dirty={dirty} onSave={() => patch({ languages, translator, translate_provider: provider, translate_model: model })}>
        {site.languages.length > 0 && site.translator && (
          <Button
            icon={<RefreshCw className="size-4" />}
            disabled={asked}
            onClick={async () => {
              await api.post(`${base(pid)}/sites/${site.id}/translate`);
              setAsked(true);
            }}
          >
            {t(asked ? "docs.translation.asked" : "docs.translation.now")}
          </Button>
        )}
      </SaveRow>
      {(translator === "deepl" || translator === "google") && <TranslatorKey pid={pid} site={site} which={translator} isOwner={isOwner} />}
    </div>
  );
}

/** A translator's API key: written once, never shown again. */
function TranslatorKey({ pid, site, which, isOwner }: { pid: number; site: DocSite; which: "deepl" | "google"; isOwner: boolean }) {
  const { t } = useTranslation();
  const refresh = useDocsRefresh(pid);
  const set = which === "deepl" ? site.deepl_key_set : site.google_key_set;
  const [value, setValue] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const store = async (key: string) => {
    setBusy(true);
    setError(null);
    try {
      await api.put(`${base(pid)}/sites/${site.id}/translator-keys`, { [which]: key });
      setValue("");
      await refresh();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-2 border-t border-line pt-4">
      <Field label={t(`docs.settings.${which}Key`)} hint={t(isOwner ? "docs.settings.keyHint" : "docs.settings.keyOwner")} group>
        <p className="flex items-center gap-2">
          <KeyRound className="size-4 text-fg-3" />
          {set ? <span className="text-ok">{t("docs.settings.keySet")}</span> : <span className="text-fg-3">{t("docs.settings.keyNotSet")}</span>}
        </p>
        {isOwner && (
          <div className="flex flex-wrap gap-2">
            <Input type="password" autoComplete="off" value={value} onChange={(e) => setValue(e.target.value)} placeholder={t(set ? "docs.settings.keyReplace" : "docs.settings.keyEnter")} className="min-w-48 flex-1" />
            <Button variant="primary" disabled={!value.trim()} loading={busy} onClick={() => store(value.trim())}>
              {t("common.save")}
            </Button>
            {set && (
              <Button variant="danger" onClick={() => store("")} disabled={busy}>
                {t("common.remove")}
              </Button>
            )}
          </div>
        )}
      </Field>
      <ErrorText error={error} />
    </div>
  );
}

// --- the doc agent --------------------------------------------------------------------------------------------

function DocAgent({ pid, site }: { pid: number; site: DocSite }) {
  const { t } = useTranslation();
  const meta = useMeta();
  const patch = usePatch(pid, site);
  const agent = site.agent;
  const [provider, setProvider] = useState(agent?.provider ?? "");
  const [model, setModel] = useState(agent?.model ?? "");
  const [effort, setEffort] = useState(agent?.effort ?? "");
  const providers = meta.data?.providers ?? [];
  const info = providers.find((p) => p.name === provider);
  const dirty = provider !== (agent?.provider ?? "") || model !== (agent?.model ?? "") || effort !== (agent?.effort ?? "");
  return (
    <div className="space-y-4">
      {agent && (
        <p className="flex flex-wrap items-center gap-2">
          <span className="font-semibold">{agent.name}</span>
          <span className="text-fg-3">@{agent.slug}</span>
          {!agent.enabled && <Badge>{t("docs.settings.agentPaused")}</Badge>}
        </p>
      )}
      {providers.length > 1 && (
        <Field label={t("team.provider")} hint={t("team.providerHint")}>
          <Select value={provider} onChange={(e) => (setProvider(e.target.value), setModel(""), setEffort(""))}>
            {providers.map((p) => (
              <option key={p.name} value={p.name}>
                {p.label}
              </option>
            ))}
          </Select>
        </Field>
      )}
      <FieldRow>
        <Field label={t("team.model")} hint={t("docs.settings.agentModelHint")}>
          <ModelSelect provider={info} value={model} onChange={setModel} />
        </Field>
        <Field label={t("team.effort")} hint={t("team.effortHint")}>
          <Select value={effort} onChange={(e) => setEffort(e.target.value)}>
            <option value="">{t("team.effortDefault")}</option>
            {(info?.efforts ?? []).map((e) => (
              <option key={e} value={e}>
                {t("settings.effortOption", { effort: e })}
              </option>
            ))}
          </Select>
        </Field>
      </FieldRow>
      <SaveRow dirty={dirty} onSave={() => patch({ agent_provider: provider, agent_model: model, agent_effort: effort })} />
      <div className="space-y-1">
        <Toggle checked={site.restructure_check} onChange={(v) => void patch({ restructure_check: v })} label={t("docs.settings.restructure")} />
        <p className="text-fg-3">{t("docs.settings.restructureHint")}</p>
      </div>
    </div>
  );
}

// --- the assistant for visitors (#119) ------------------------------------------------------------------------

function AssistantSettings({ pid, site }: { pid: number; site: DocSite }) {
  const { t } = useTranslation();
  const meta = useMeta();
  const patch = usePatch(pid, site);
  const [enabled, setEnabled] = useState(site.assistant_enabled);
  const [model, setModel] = useState(site.assistant_model);
  const [effort, setEffort] = useState(site.assistant_effort);
  const [code, setCode] = useState(site.assistant_code);
  const [messages, setMessages] = useState(String(site.assistant_daily_messages));
  const [cost, setCost] = useState(String(site.assistant_daily_cost));
  const [tokens, setTokens] = useState(String(site.assistant_max_tokens));
  const [confirming, setConfirming] = useState(false);
  // It runs on Claude only (services/docs.ASSISTANT_PROVIDERS): another provider's run always has a shell.
  const claude = (meta.data?.providers ?? []).find((p) => p.name === "claude_code");
  const models = claude?.models ?? [];
  const small = models.filter((m) => m.tier === "small");
  const fallback = small.find((m) => m.id.includes("haiku")) ?? small[0] ?? models[models.length - 1];
  const efforts = claude?.efforts ?? [];
  const num = (v: string) => (v.trim() === "" ? NaN : Number(v));
  const valid = Number.isInteger(num(messages)) && num(messages) >= 1 && num(cost) >= 0 && Number.isInteger(num(tokens)) && num(tokens) >= 128 && num(tokens) <= 8192;
  const dirty =
    enabled !== site.assistant_enabled ||
    model !== site.assistant_model ||
    effort !== site.assistant_effort ||
    code !== site.assistant_code ||
    num(messages) !== site.assistant_daily_messages ||
    num(cost) !== site.assistant_daily_cost ||
    num(tokens) !== site.assistant_max_tokens;
  return (
    <div className="space-y-5">
      <div className="space-y-1">
        <Toggle checked={enabled} onChange={setEnabled} label={t("docs.assistant.enabled")} />
        <p className="text-fg-3">{t("docs.assistant.enabledHint")}</p>
        {enabled && !site.published && <p className="text-you-fg">{t("docs.assistant.draft")}</p>}
      </div>
      <Field group label={t("docs.assistant.model")} hint={t("docs.assistant.modelHint")}>
        <div data-part="assistant-model" className="flex flex-wrap items-start gap-2">
          <div className="min-w-0 flex-1 basis-48">
            <Select aria-label={t("docs.assistant.model")} value={model} onChange={(e) => setModel(e.target.value)}>
              <option value="">{t("projects.askModelSmall", { model: fallback?.label ?? "Claude Haiku" })}</option>
              {models.map((m) => (
                <option key={m.id} value={m.id}>
                  {m.label}
                </option>
              ))}
              {model && !models.some((m) => m.id === model) && <option value={model}>{model}</option>}
            </Select>
          </div>
          <div className="w-full sm:w-48">
            <Select aria-label={t("settings.defaultEffort")} value={effort} onChange={(e) => setEffort(e.target.value)}>
              <option value="">{t("team.effortDefault")}</option>
              {efforts.map((e) => (
                <option key={e} value={e}>
                  {t("settings.effortOption", { effort: e })}
                </option>
              ))}
            </Select>
          </div>
        </div>
      </Field>
      <div className="space-y-2">
        <Toggle checked={code} onChange={(v) => (v ? setConfirming(true) : setCode(false))} label={t("docs.assistant.code")} />
        <p className="text-fg-3">{t("docs.assistant.codeHint")}</p>
        <div role="note" data-part="assistant-warning" className={clsx("flex gap-2.5 rounded-sm p-3 ring-1 ring-inset", code ? "bg-you-bg text-fg ring-you" : "text-fg-2 ring-line-2")}>
          <ShieldAlert className={clsx("mt-0.5 size-4 shrink-0", code ? "text-you-fg" : "text-fg-3")} aria-hidden />
          <div className="space-y-1">
            <p className="font-semibold">{t("docs.assistant.warningTitle")}</p>
            <p>{t("docs.assistant.warning")}</p>
          </div>
        </div>
      </div>
      <Field group label={t("docs.assistant.limits")} hint={t("docs.assistant.limitsHint")}>
        <div className="grid gap-3 sm:grid-cols-3">
          <label className="space-y-1">
            <span className="block text-fg-2">{t("docs.assistant.dailyMessages")}</span>
            <Input type="number" inputMode="numeric" min={1} max={100000} value={messages} onChange={(e) => setMessages(e.target.value)} />
            <span className="block text-fg-3">{t("docs.assistant.dailyMessagesHint")}</span>
          </label>
          <label className="space-y-1">
            <span className="block text-fg-2">{t("docs.assistant.dailyCost")}</span>
            <Input type="number" inputMode="decimal" min={0} step={0.5} value={cost} onChange={(e) => setCost(e.target.value)} />
            <span className="block text-fg-3">{t("docs.assistant.dailyCostHint")}</span>
          </label>
          <label className="space-y-1">
            <span className="block text-fg-2">{t("docs.assistant.maxTokens")}</span>
            <Input type="number" inputMode="numeric" min={128} max={8192} step={128} value={tokens} onChange={(e) => setTokens(e.target.value)} />
            <span className="block text-fg-3">{t("docs.assistant.maxTokensHint")}</span>
          </label>
        </div>
      </Field>
      <SaveRow
        dirty={dirty && valid}
        onSave={() =>
          patch({
            assistant_enabled: enabled,
            assistant_provider: "",
            assistant_model: model,
            assistant_effort: effort,
            assistant_code: code,
            assistant_daily_messages: num(messages),
            assistant_daily_cost: num(cost),
            assistant_max_tokens: num(tokens),
          })
        }
      >
        {site.published && (
          <a href={site.url} target="_blank" rel="noreferrer" className="inline-flex items-center gap-1.5 text-fg-2 hover:text-fg">
            <ExternalLink className="size-4" /> {t("docs.assistant.try")}
          </a>
        )}
      </SaveRow>
      <Modal
        open={confirming}
        onClose={() => setConfirming(false)}
        width="max-w-lg"
        title={
          <span className="flex items-center gap-2">
            <ShieldAlert className="size-4 text-you-fg" /> {t("docs.assistant.confirmTitle")}
          </span>
        }
        footer={
          <>
            <Button variant="ghost" onClick={() => setConfirming(false)}>
              {t("common.cancel")}
            </Button>
            <Button
              variant="danger"
              onClick={() => {
                setCode(true);
                setConfirming(false);
              }}
            >
              {t("docs.assistant.confirmYes")}
            </Button>
          </>
        }
      >
        <div className="space-y-3">
          <p>{t("docs.assistant.warning")}</p>
          <p className="text-fg-2">{t("docs.assistant.confirmText")}</p>
        </div>
      </Modal>
    </div>
  );
}

// --- own domain -----------------------------------------------------------------------------------------------

function CopyValue({ value }: { value: string }) {
  const { t } = useTranslation();
  const [done, setDone] = useState(false);
  return (
    <span className="flex min-w-0 items-center gap-1">
      <code className="min-w-0 truncate rounded-sm bg-sunk px-1.5 py-0.5">{value}</code>
      <button
        type="button"
        title={t("common.copy")}
        aria-label={t("common.copy")}
        className="inline-flex size-7 shrink-0 items-center justify-center rounded-sm text-fg-3 hover:bg-sunk hover:text-fg"
        onClick={async () => {
          await navigator.clipboard?.writeText(value).catch(() => undefined);
          setDone(true);
          window.setTimeout(() => setDone(false), 1500);
        }}
      >
        {done ? <Check className="size-3.5 text-ok" /> : <Copy className="size-3.5" />}
      </button>
    </span>
  );
}

function Step({ n, title, children, done }: { n: number; title: string; children: ReactNode; done?: boolean }) {
  return (
    <li className="flex gap-3">
      <span className={clsx("mt-0.5 inline-flex size-6 shrink-0 items-center justify-center rounded-full font-semibold", done ? "bg-ok text-bg" : "ring-1 ring-line-2 ring-inset")}>
        {done ? <Check className="size-3.5" /> : n}
      </span>
      <div className="min-w-0 flex-1 space-y-2">
        <p className="font-semibold">{title}</p>
        {children}
      </div>
    </li>
  );
}

function Domain({ pid, site, isOwner }: { pid: number; site: DocSite; isOwner: boolean }) {
  const { t } = useTranslation();
  const refresh = useDocsRefresh(pid);
  const setup = useDocDomain(pid, site.id, !!site.domain);
  const [domain, setDomain] = useState(site.domain ?? "");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  const [verify, setVerify] = useState<{ verified: boolean; detail: string } | null>(null);
  const [checking, setChecking] = useState(false);
  const [cfToken, setCfToken] = useState("");
  const [proxied, setProxied] = useState(true);
  const [cf, setCf] = useState<{ zone: string; record: string; target: string; created: boolean } | null>(null);
  const [cfBusy, setCfBusy] = useState(false);
  const [cfError, setCfError] = useState("");
  const info = setup.data;
  const verified = !!site.domain_verified_at;
  const saveDomain = async (value: string) => {
    setBusy(true);
    setError(null);
    try {
      await api.put(`${base(pid)}/sites/${site.id}/domain`, { domain: value });
      setVerify(null);
      await refresh();
    } catch (e) {
      setError(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="space-y-5">
      <Field label={t("docs.domain.label")} hint={t(isOwner ? "docs.domain.labelHint" : "docs.domain.ownerOnly")} group>
        <div className="flex flex-wrap gap-2">
          <Input value={domain} onChange={(e) => setDomain(e.target.value.trim().toLowerCase())} placeholder="docs.example.com" disabled={!isOwner} className="min-w-48 flex-1" />
          {isOwner && (
            <Button variant="primary" loading={busy} disabled={domain === (site.domain ?? "")} onClick={() => saveDomain(domain)}>
              {t("common.save")}
            </Button>
          )}
          {isOwner && site.domain && (
            <Button variant="danger" disabled={busy} onClick={() => (setDomain(""), saveDomain(""))}>
              {t("common.remove")}
            </Button>
          )}
        </div>
      </Field>
      <ErrorText error={error} />
      {site.domain && (
        <div className="space-y-4">
          <p className={clsx("flex items-center gap-2 font-medium", verified ? "text-ok" : "text-fg-2")}>
            {verified ? <Check className="size-4" /> : <Globe2 className="size-4" />}
            {verified ? t("docs.domain.verified", { when: formatDateTime(site.domain_verified_at) }) : t("docs.domain.notVerified")}
          </p>
          {setup.isLoading || !info ? (
            <Loader2 className="size-4 animate-spin text-fg-3" />
          ) : (
            <ol className="space-y-5">
              <Step n={1} title={t("docs.domain.step1")} done={verified}>
                <p className="text-fg-2">{t("docs.domain.step1Hint")}</p>
                <div className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-x-4 gap-y-1.5 rounded-sm p-3 ring-1 ring-line ring-inset">
                  <span className="text-fg-3">{t("docs.domain.type")}</span>
                  <code>CNAME</code>
                  <span className="text-fg-3">{t("docs.domain.name")}</span>
                  <CopyValue value={info.domain} />
                  <span className="text-fg-3">{t("docs.domain.target")}</span>
                  <CopyValue value={info.target} />
                </div>
                <details className="text-fg-2">
                  <summary className="cursor-pointer font-medium text-fg">{t("docs.domain.cloudflareManual")}</summary>
                  <ul className="mt-2 list-disc space-y-1 pl-5">
                    <li>{t("docs.domain.cfDns")}</li>
                    <li>{t("docs.domain.cfTunnel", { domain: info.domain })}</li>
                    <li>{t("docs.domain.cfSsl")}</li>
                  </ul>
                </details>
              </Step>
              {isOwner && (
                <Step n={2} title={t("docs.domain.step2")}>
                  <p className="text-fg-2">{t("docs.domain.step2Hint")}</p>
                  <div className="flex flex-wrap items-center gap-2">
                    <Input
                      type="password"
                      autoComplete="off"
                      value={cfToken}
                      onChange={(e) => setCfToken(e.target.value)}
                      placeholder={t(info.cloudflare_token_set ? "docs.domain.tokenStored" : "docs.domain.token")}
                      className="min-w-48 flex-1"
                    />
                    <Toggle checked={proxied} onChange={setProxied} label={t("docs.domain.proxied")} />
                  </div>
                  <div className="flex flex-wrap items-center gap-2">
                    <Button
                      icon={<Cloud className="size-4" />}
                      loading={cfBusy}
                      disabled={!cfToken.trim() && !info.cloudflare_token_set}
                      onClick={async () => {
                        setCfBusy(true);
                        setCfError("");
                        try {
                          setCf(await api.post(`${base(pid)}/sites/${site.id}/domain/cloudflare`, { token: cfToken.trim() || null, proxied }));
                          setCfToken("");
                          await refresh();
                        } catch (e) {
                          setCfError(errorMessage(e));
                        } finally {
                          setCfBusy(false);
                        }
                      }}
                    >
                      {t("docs.domain.createRecord")}
                    </Button>
                    {info.cloudflare_token_set && (
                      <Button
                        variant="ghost"
                        icon={<Trash2 className="size-4" />}
                        onClick={async () => {
                          await api.del(`${base(pid)}/sites/${site.id}/domain/cloudflare`);
                          await refresh();
                        }}
                      >
                        {t("docs.domain.forgetToken")}
                      </Button>
                    )}
                  </div>
                  {cf && <p className="text-ok">{t(cf.created ? "docs.domain.recordCreated" : "docs.domain.recordChanged", { record: cf.record, target: cf.target, zone: cf.zone })}</p>}
                  {cfError && <ErrorText error={cfError} />}
                </Step>
              )}
              <Step n={isOwner ? 3 : 2} title={t("docs.domain.step3")} done={verified}>
                <p className="text-fg-2">{t("docs.domain.step3Hint")}</p>
                <div className="grid grid-cols-[auto_minmax(0,1fr)] items-center gap-x-4 gap-y-1.5 rounded-sm p-3 ring-1 ring-line ring-inset">
                  <span className="text-fg-3">{t("docs.domain.verifyUrl")}</span>
                  <CopyValue value={info.verify_url} />
                  <span className="text-fg-3">{t("docs.domain.expect")}</span>
                  <CopyValue value={info.token} />
                </div>
                <div className="flex flex-wrap items-center gap-2">
                  <Button
                    variant={verified ? "secondary" : "primary"}
                    loading={checking}
                    icon={<RefreshCw className="size-4" />}
                    onClick={async () => {
                      setChecking(true);
                      try {
                        setVerify(await api.post(`${base(pid)}/sites/${site.id}/domain/verify`));
                        await refresh();
                      } catch (e) {
                        setVerify({ verified: false, detail: errorMessage(e) });
                      } finally {
                        setChecking(false);
                      }
                    }}
                  >
                    {t("docs.domain.check")}
                  </Button>
                  {verify && <span className={verify.verified ? "text-ok" : "text-you-fg"}>{verify.verified ? t("docs.domain.checkOk") : verify.detail || t("docs.domain.checkFailed")}</span>}
                </div>
              </Step>
            </ol>
          )}
        </div>
      )}
    </div>
  );
}

// --- delete ---------------------------------------------------------------------------------------------------

function Delete({ pid, site, onDeleted }: { pid: number; site: DocSite; onDeleted: () => void }) {
  const { t } = useTranslation();
  const refresh = useDocsRefresh(pid);
  const [typed, setTyped] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<unknown>(null);
  return (
    <div className="space-y-3">
      <p className="text-fg-2">{t("docs.settings.deleteWhat", { pages: site.pages })}</p>
      <Field label={t("docs.settings.deleteType", { name: site.name })}>
        <Input value={typed} onChange={(e) => setTyped(e.target.value)} />
      </Field>
      <Button
        variant="danger"
        icon={<Trash2 className="size-4" />}
        disabled={typed.trim() !== site.name}
        loading={busy}
        onClick={async () => {
          setBusy(true);
          try {
            await api.del(`${base(pid)}/sites/${site.id}`);
            onDeleted();
            await refresh();
          } catch (e) {
            setError(e);
            setBusy(false);
          }
        }}
      >
        {t("docs.settings.deleteButton")}
      </Button>
      <ErrorText error={error} />
    </div>
  );
}

