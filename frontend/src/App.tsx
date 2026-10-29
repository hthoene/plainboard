import { useQueryClient } from "@tanstack/react-query";
import { useEffect } from "react";
import { qk, useAuth } from "./api/queries";
import { Spinner } from "./components/ui";
import { Login } from "./features/auth/Login";
import { Shell } from "./features/layout/Shell";

export default function App() {
  const auth = useAuth();
  const qc = useQueryClient();

  useEffect(() => {
    const onUnauthorized = () => qc.invalidateQueries({ queryKey: qk.auth });
    window.addEventListener("plainboard:unauthorized", onUnauthorized);
    return () => window.removeEventListener("plainboard:unauthorized", onUnauthorized);
  }, [qc]);

  if (auth.isLoading) {
    return (
      <div className="flex h-full items-center justify-center">
        <Spinner className="size-6" />
      </div>
    );
  }
  if (auth.data && !auth.data.authenticated) return <Login />;
  return <Shell />;
}
