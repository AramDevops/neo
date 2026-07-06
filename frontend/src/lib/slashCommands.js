export const SLASH_COMMANDS = [
  { cmd: "/help", hint: "list available commands" },
  { cmd: "/clear", hint: "wipe this terminal's chat history" },
  { cmd: "/role", hint: "show or set this agent's durable role" },
  { cmd: "/scope", hint: "show or set enforced write paths, e.g. /scope server/*" },
  { cmd: "/ls", hint: "list the workspace directory" },
  { cmd: "/tree", hint: "show the workspace tree" },
  { cmd: "/pwd", hint: "print the workspace directory" },
  { cmd: "/cat", hint: "print a workspace file" },
  { cmd: "/mkdir", hint: "create directories" },
  { cmd: "/touch", hint: "create empty files" },
  { cmd: "/cp", hint: "copy a file (-r for directories)" },
  { cmd: "/mv", hint: "move or rename a path" },
  { cmd: "/rm", hint: "delete files (-r for directories)" },
  { cmd: "/grep", hint: "regex search file contents" },
  { cmd: "/find", hint: "find files by glob, e.g. /find *.py" },
  { cmd: "/model", hint: "show or switch the model" },
  { cmd: "/tools", hint: "tool catalog by category" },
  { cmd: "/access", hint: "computer-control permission status" },
  { cmd: "/access grant", hint: "grant timed computer control" },
  { cmd: "/access revoke", hint: "revoke the active grant" },
  { cmd: "/access full", hint: "enable full control" },
  { cmd: "/access ask", hint: "switch back to ask mode" },
  { cmd: "/status", hint: "Neo metrics snapshot" },
  { cmd: "/workspace", hint: "active workspace status" },
  { cmd: "/checkpoints", hint: "list workspace snapshots" },
  { cmd: "/rollback", hint: "undo a run's file changes (optionally by id)" }
];

export function slashCommandMatches(draft) {
  const text = String(draft || "");
  if (!text.startsWith("/") || text.includes("\n")) return [];
  const base = text.trimEnd().toLowerCase();
  return SLASH_COMMANDS.filter((item) => item.cmd.startsWith(base));
}
