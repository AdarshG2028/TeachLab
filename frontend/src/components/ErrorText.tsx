export function ErrorText({ error }: { error: unknown }) {
  if (!error) return null;
  const msg = error instanceof Error ? error.message : String(error);
  return <p className="my-2 rounded bg-black/5 p-2 text-sm text-red-700">{msg}</p>;
}
