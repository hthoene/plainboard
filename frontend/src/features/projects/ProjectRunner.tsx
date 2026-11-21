import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api } from "../../api/client";
import type { ProjectRunner } from "../../api/types";
import { Button, ErrorText, Field, Select } from "../../components/ui";

/** Which runner the project's agents, jobs and check runs use: the built-in one or an external one
 *  (Settings > Runners). Asks first, then saves; refused while something of the project runs. */
export function ProjectRunnerEditor({ projectId, isOwner }: { projectId: number; isOwner: boolean }) {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const key = ["project-runner", projectId];
  const data = useQuery({ queryKey: key, queryFn: () => api.get<ProjectRunner>(`/projects/${projectId}/runner`) }).data;
  const [pending, setPending] = useState<number | null | undefined>(undefined); // undefined: nothing chosen
  const [error, setError] = useState<unknown>(null);
  if (!data) return null;

  const nameOf = (id: number | null) => {
    const r = data.runners.find((x) => x.id === id);
    return r ? (r.id === null ? t("projectRunner.builtin") : r.name) : "";
  };
  const apply = async () => {
    if (pending === undefined) return;
    setError(null);
    try {
      qc.setQueryData(key, await api.put<ProjectRunner>(`/projects/${projectId}/runner`, { runner_id: pending }));
      await qc.invalidateQueries({ queryKey: ["projects"] });
      setPending(undefined);
    } catch (err) {
      setError(err);
    }
  };

  return (
    <div className="space-y-4">
      <Field label={t("projectRunner.label")} hint={data.runners.length > 1 ? t("projectRunner.hint") : t("projectRunner.hintNone")}>
        <Select
          value={pending === undefined ? (data.runner_id ?? "") : (pending ?? "")}
          onChange={(e) => {
            const id = e.target.value ? Number(e.target.value) : null;
            setError(null);
            setPending(id === data.runner_id ? undefined : id);
          }}
          disabled={!isOwner || !!data.busy}
        >
          {data.runners.map((r) => (
            <option key={r.id ?? "builtin"} value={r.id ?? ""}>
              {r.id === null ? t("projectRunner.builtin") : r.connected ? r.name : t("projectRunner.away", { name: r.name })}
            </option>
          ))}
        </Select>
      </Field>
      {data.busy && <p className="text-you-fg">{t("projectRunner.busy")}</p>}
      {pending !== undefined && (
        <div className="space-y-3 rounded-sm bg-sunk p-3">
          <p className="font-medium">{t("projectRunner.confirmTitle", { name: nameOf(pending) })}</p>
          <p className="text-fg-2">{t("projectRunner.confirmBody")}</p>
          <div className="flex gap-2">
            <Button size="md" variant="primary" onClick={apply}>
              {t("projectRunner.confirmYes")}
            </Button>
            <Button size="md" onClick={() => setPending(undefined)}>
              {t("common.cancel")}
            </Button>
          </div>
        </div>
      )}
      <ErrorText error={error} />
    </div>
  );
}
