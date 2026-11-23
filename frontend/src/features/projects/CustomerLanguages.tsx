import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import { qk } from "../../api/queries";
import type { Project } from "../../api/types";
import { Badge, Button, ErrorText, Select } from "../../components/ui";

/** Codes offered when adding a customer language (the same list as services/customer_texts.LANGUAGES). */
export const LANGUAGE_CODES = ["en", "de", "fr", "es", "it", "nl", "pl", "pt", "sv", "da", "no", "fi", "cs", "sk", "hu", "ro", "el", "tr", "ru", "uk", "ar", "he", "hi", "zh", "ja", "ko"];

/** A language's name in the UI's language, e.g. "German" for de. */
export function languageName(code: string, uiLanguage: string): string {
  try {
    return new Intl.DisplayNames([uiLanguage], { type: "language" }).of(code) ?? code;
  } catch {
    return code;
  }
}

/** The languages of a project's customer texts: the first is the default. Every change is stored at once.
 * Removing a language keeps its texts on the tickets; they come back when it is added again. */
export function CustomerLanguages({ project }: { project: Project }) {
  const { t, i18n } = useTranslation();
  const qc = useQueryClient();
  const languages = project.customer_languages?.length ? project.customer_languages : ["en"];
  const choices = LANGUAGE_CODES.filter((code) => !languages.includes(code));
  const [adding, setAdding] = useState(choices[0] ?? "");
  const [error, setError] = useState<unknown>(null);
  const name = (code: string) => languageName(code, i18n.language);

  const store = async (next: string[]) => {
    setError(null);
    try {
      await api.patch<Project>(`/projects/${project.id}`, { customer_languages: next });
      await qc.invalidateQueries({ queryKey: qk.projects });
      qc.invalidateQueries({ queryKey: qk.tasks(project.id) });
    } catch (err) {
      setError(err);
    }
  };

  const remove = async (code: string) => {
    const counts = await api.get<Record<string, number>>(`/projects/${project.id}/customer-texts`).catch(() => ({}) as Record<string, number>);
    const count = counts[code] ?? 0;
    if (count && !confirm(t("customerLanguages.confirmRemove", { language: name(code), count }))) return;
    await store(languages.filter((l) => l !== code));
  };

  const add = async () => {
    if (!adding) return;
    await store([...languages, adding]);
    setAdding(LANGUAGE_CODES.find((code) => code !== adding && !languages.includes(code)) ?? "");
  };

  return (
    <div className="space-y-4" data-part="customer-languages">
      <ul className="border-t border-line">
        {languages.map((code, index) => (
          <li key={code} className="flex flex-wrap items-center gap-2 border-b border-line py-2">
            <span className="min-w-0 flex-1">
              {name(code)} <span className="text-fg-3">{code}</span>
            </span>
            {index === 0 ? (
              <Badge>{t("customerLanguages.default")}</Badge>
            ) : (
              <>
                <Button size="sm" variant="ghost" onClick={() => store([code, ...languages.filter((l) => l !== code)])}>
                  {t("customerLanguages.makeDefault")}
                </Button>
                <Button size="sm" variant="ghost" onClick={() => remove(code)}>
                  {t("common.remove")}
                </Button>
              </>
            )}
          </li>
        ))}
      </ul>
      {choices.length > 0 && (
        <div className="flex flex-wrap items-center gap-2">
          <Select aria-label={t("customerLanguages.add")} className="w-56" value={adding} onChange={(e) => setAdding(e.target.value)}>
            {choices.map((code) => (
              <option key={code} value={code}>
                {`${name(code)} (${code})`}
              </option>
            ))}
          </Select>
          <Button onClick={add} disabled={!adding}>
            {t("customerLanguages.add")}
          </Button>
        </div>
      )}
      <p className="text-fg-3">{t("customerLanguages.hint")}</p>
      {error ? <ErrorText error={error} /> : null}
    </div>
  );
}
