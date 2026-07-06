import { slashCommandMatches } from "../lib/slashCommands.js";

export default function SlashMenu({ draft, onPick }) {
  const text = draft || "";
  const matches = slashCommandMatches(text).filter((item) => item.cmd !== text.trim());
  if (!matches.length) return null;
  return (
    <div className="slash-menu">
      {matches.map((item) => (
        <button
          type="button"
          key={item.cmd}
          onMouseDown={(event) => {
            event.preventDefault();
            onPick(item.cmd);
          }}
        >
          <code>{item.cmd}</code>
          <span>{item.hint}</span>
        </button>
      ))}
    </div>
  );
}
