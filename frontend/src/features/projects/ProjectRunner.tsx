import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import type { ProjectRunner } from "../../api/types";
import { ErrorText, Field, Select } from "../../components/ui";

/** Which runner the project's agents, jobs and check runs use: the built-in one or an external one
 *  (Settings > Runners). Saved at once; refused while something of the project runs. */
export function ProjectRunnerEditor({ projectId, isOwner }: { projectId: number; isOwner: boolean }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const key = ["project-runner", projectId];
  const data = useQuery({ queryKey: key, queryFn: () => api.get<ProjectRunner>(`/projects/${projectId}/runner`) }).data;
  const [error, setError] = useState<unknown>(null);
  if (!data) return null;

  const choose = async (value: string) => {
    const runner_id = value ? Number(value) : null;
    if (runner_id === data.runner_id || !confirm(t("projectRunner.confirm"))) return;
    setError(null);
    try {
      qc.setQueryData(key, await api.put<ProjectRunner>(`/projects/${projectId}/runner`, { runner_id }));
      await qc.invalidateQueries({ queryKey: ["projects"] });
    } catch (err) {
      setError(err);
    }
  };

  return (
    <div className="space-y-4">
      <Field label={t("projectRunner.label")} hint={t("projectRunner.hint")}>
        <Select value={data.runner_id ?? ""} onChange={(e) => choose(e.target.value)} disabled={!isOwner || !!data.busy}>
          {data.runners.map((r) => (
            <option key={r.id ?? "builtin"} value={r.id ?? ""}>
              {r.id === null ? t("projectRunner.builtin") : r.connected ? r.name : t("projectRunner.away", { name: r.name })}
            </option>
          ))}
        </Select>
      </Field>
      {data.busy && <p className="text-fg-2">{data.busy}</p>}
      <p className="text-fg-3">{t("projectRunner.warning")}</p>
      <ErrorText error={error} />
    </div>
  );
}
