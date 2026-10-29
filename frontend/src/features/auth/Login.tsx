import { useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import { useTranslation } from "react-i18next";
import { api, errorMessage } from "../../api/client";
import { Button, Input } from "../../components/ui";

export function Login() {
  const { t } = useTranslation();
  const qc = useQueryClient();
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.post("/auth/login", { password });
      await qc.invalidateQueries();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="flex h-full items-center justify-center p-6">
      <form onSubmit={submit} className="w-full max-w-xs space-y-4">
        <div className="flex items-center gap-2 text-lg font-semibold">
          <Logo /> PlainBoard
        </div>
        <Input type="password" autoFocus placeholder={t("auth.password")} value={password} onChange={(e) => setPassword(e.target.value)} />
        {error && <p className="text-sm text-red-600">{error}</p>}
        <Button type="submit" variant="primary" className="w-full" loading={busy}>
          {t("auth.signIn")}
        </Button>
      </form>
    </div>
  );
}

export function Logo({ className = "size-5" }: { className?: string }) {
  return (
    <svg viewBox="0 0 32 32" className={className} aria-hidden>
      <rect width="32" height="32" rx="7" className="fill-indigo-600" />
      <rect x="7" y="8" width="5" height="16" rx="1.5" fill="white" />
      <rect x="14" y="8" width="5" height="11" rx="1.5" fill="white" opacity=".8" />
      <rect x="21" y="8" width="5" height="7" rx="1.5" fill="white" opacity=".6" />
    </svg>
  );
}
