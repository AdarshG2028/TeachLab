"use client";

import { useEffect, useState } from "react";
import { getUserId, setUserId } from "@/lib/api";

/** Local-development identity switcher (AUTH_PROVIDER=local). Replace with the real auth UI later. */
export function UserSwitcher({ onChange }: { onChange?: (id: string) => void }) {
  const [id, setId] = useState("local-teacher");
  useEffect(() => setId(getUserId()), []);
  return (
    <form
      className="flex items-center gap-2 text-sm"
      onSubmit={(e) => {
        e.preventDefault();
        setUserId(id.trim() || "local-teacher");
        onChange?.(id.trim());
      }}
    >
      <span className="text-muted">Signed in as (dev)</span>
      <input className="input w-40" value={id} onChange={(e) => setId(e.target.value)} aria-label="user id" />
      <button className="btn-secondary" type="submit">Switch</button>
    </form>
  );
}
