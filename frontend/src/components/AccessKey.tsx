"use client";

import { useEffect, useState } from "react";
import { getClientId, setClientId } from "@/lib/api";

/**
 * No accounts: workspaces are tied to a private key kept in this browser. This shows the key so it can be saved,
 * and lets the user paste a key from another browser/device to open those workspaces here.
 */
export function AccessKey({ onChange }: { onChange?: () => void }) {
  const [key, setKey] = useState("");
  const [input, setInput] = useState("");
  const [msg, setMsg] = useState("");
  useEffect(() => setKey(getClientId()), []);

  return (
    <details className="text-sm">
      <summary className="cursor-pointer text-white/70 hover:text-white">
        No account needed · your workspaces are saved in this browser. <u>Access key</u>
      </summary>
      <div className="mt-3 space-y-3 rounded-xl bg-white/5 p-3 ring-1 ring-white/10">
        <p className="muted text-xs">
          Your key is the only thing that unlocks your workspaces. Save it somewhere safe: clearing this browser&apos;s
          data without it loses access, and anyone who has it can open your workspaces.
        </p>
        <div className="flex gap-2">
          <input readOnly className="input font-mono text-xs" value={key} aria-label="Your access key" />
          <button className="btn-secondary" onClick={async () => {
            await navigator.clipboard?.writeText(key);
            setMsg("Copied.");
          }}>Copy</button>
        </div>
        <form className="flex gap-2" onSubmit={(e) => {
          e.preventDefault();
          if (setClientId(input)) {
            setKey(getClientId());
            setInput("");
            setMsg("Key restored.");
            onChange?.();
          } else {
            setMsg("That is not a valid access key.");
          }
        }}>
          <input className="input font-mono text-xs" value={input} onChange={(e) => setInput(e.target.value)}
            placeholder="Paste a key from another browser to open its workspaces" aria-label="Restore access key" />
          <button className="btn-accent" disabled={!input.trim()}>Restore</button>
        </form>
        {msg && <p className="text-xs text-white/80" role="status">{msg}</p>}
      </div>
    </details>
  );
}
