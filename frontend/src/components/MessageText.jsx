import { memo } from "react";

function parseSources(content) {
  const text = String(content || "");
  const match = text.match(/\n\s*Sources:\s*\n/i);
  if (!match) return { body: text, sources: [] };
  const body = text.slice(0, match.index).trimEnd();
  const sourceText = text.slice(match.index + match[0].length);
  const sources = sourceText.split(/\n+/).map((line) => {
    const cleaned = line.replace(/^\s*[-*]\s*/, "").trim();
    const urlMatch = cleaned.match(/https?:\/\/\S+/);
    if (!urlMatch) return null;
    const url = urlMatch[0].replace(/[),.;]+$/, "");
    const title = cleaned.slice(0, urlMatch.index).replace(/:\s*$/, "").trim() || url;
    return { title, url };
  }).filter(Boolean);
  return { body, sources };
}

function collapsePastedText(content) {
  return String(content || "").replace(
    /\n?\[Neo pasted text: ([^\]\n]+)\]\n<<<neo-paste\n[\s\S]*?\nneo-paste>>>/g,
    (_match, label) => `\n[pasted text: ${label}]`
  ).trimStart();
}

export function pasteTitle(text) {
  const firstLine = String(text || "").split(/\r?\n/).map((line) => line.trim()).find(Boolean) || "pasted text";
  const clean = firstLine.replace(/\s+/g, " ").replace(/[\[\]]/g, "").slice(0, 52);
  return clean.length < firstLine.length ? `${clean}...` : clean;
}

export function pastedBlock(attachment) {
  const title = attachment.title || "pasted text";
  const chars = String(attachment.content || "").length;
  return `[Neo pasted text: ${title} | ${chars} chars]\n<<<neo-paste\n${attachment.content || ""}\nneo-paste>>>`;
}

function sourceDomain(url) {
  try {
    return new URL(url).hostname.replace(/^www\./, "");
  } catch {
    return url;
  }
}

function faviconUrl(url) {
  try {
    const domain = new URL(url).hostname;
    return `https://www.google.com/s2/favicons?domain=${encodeURIComponent(domain)}&sz=32`;
  } catch {
    return "";
  }
}

function safeHttpUrl(url) {
  try {
    const parsed = new URL(url);
    return ["http:", "https:"].includes(parsed.protocol) ? parsed.toString() : "";
  } catch {
    return "";
  }
}

function trimUrlPunctuation(url) {
  return String(url || "").replace(/[),.;!?]+$/, "");
}

function renderInline(text, keyPrefix) {
  const source = String(text || "");
  const pattern = /(\*\*([^*]+)\*\*|`([^`]+)`|\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)|(https?:\/\/[^\s<]+))/g;
  const parts = [];
  let lastIndex = 0;
  let match;
  let index = 0;

  while ((match = pattern.exec(source)) !== null) {
    if (match.index > lastIndex) {
      parts.push(source.slice(lastIndex, match.index));
    }
    const key = `${keyPrefix}-inline-${index}`;
    if (match[2]) {
      parts.push(<strong className="md-strong" key={key}>{match[2]}</strong>);
    } else if (match[3]) {
      parts.push(<code className="md-code" key={key}>{match[3]}</code>);
    } else if (match[4] && match[5]) {
      const url = safeHttpUrl(trimUrlPunctuation(match[5]));
      parts.push(url ? (
        <a className="md-link" href={url} target="_blank" rel="noreferrer" key={key}>{match[4]}</a>
      ) : match[0]);
    } else if (match[6]) {
      const cleanUrl = trimUrlPunctuation(match[6]);
      const url = safeHttpUrl(cleanUrl);
      parts.push(url ? (
        <a className="md-link" href={url} target="_blank" rel="noreferrer" key={key}>{cleanUrl}</a>
      ) : match[0]);
    }
    lastIndex = pattern.lastIndex;
    index += 1;
  }

  if (lastIndex < source.length) {
    parts.push(source.slice(lastIndex));
  }
  return parts.length ? parts : source;
}

function isSpecialMarkdownLine(line) {
  return /^```/.test(line)
    || /^#{1,4}\s+/.test(line)
    || /^\s*(?:[-*]|\d+[.)])\s+/.test(line);
}

function renderMarkdown(text) {
  const lines = String(text || "").replace(/\r\n/g, "\n").split("\n");
  const blocks = [];
  let index = 0;
  let blockIndex = 0;
  // Absolute backstop: rendering runs on every typewriter tick, so this loop
  // must be total no matter what partial markdown arrives. If any future
  // branch fails to consume a line, bail to plain text instead of freezing.
  let guard = lines.length * 4 + 64;

  while (index < lines.length) {
    if ((guard -= 1) < 0) {
      blocks.push(<span className="md-paragraph" key={`md-${blockIndex}`}><span className="md-paragraph-line">{lines.slice(index).join("\n")}</span></span>);
      break;
    }
    const line = lines[index];
    const key = `md-${blockIndex}`;

    if (!line.trim()) {
      blocks.push(<span className="md-gap" key={key} />);
      index += 1;
      blockIndex += 1;
      continue;
    }

    const fence = line.match(/^```\s*([A-Za-z0-9_+.-]+)?\s*$/);
    if (fence) {
      const language = fence[1] || "";
      const codeLines = [];
      index += 1;
      while (index < lines.length && !/^```\s*$/.test(lines[index])) {
        codeLines.push(lines[index]);
        index += 1;
      }
      if (index < lines.length) index += 1;
      blocks.push(
        <span className="md-codeblock" key={key}>
          {language && <span className="md-codeblock-lang">{language}</span>}
          <code>{codeLines.join("\n")}</code>
        </span>
      );
      blockIndex += 1;
      continue;
    }

    const heading = line.match(/^(#{1,4})\s+(.+)$/);
    if (heading) {
      blocks.push(
        <span className={`md-heading level-${heading[1].length}`} key={key}>
          {renderInline(heading[2], key)}
        </span>
      );
      index += 1;
      blockIndex += 1;
      continue;
    }

    if (/^\s*(?:[-*]|\d+[.)])\s+/.test(line)) {
      const items = [];
      while (index < lines.length) {
        const item = lines[index].match(/^\s*([-*]|\d+[.)])\s+(.+)$/);
        if (!item) break;
        items.push({ marker: item[1], text: item[2] });
        index += 1;
      }
      if (!items.length) {
        // A bare marker with no text yet ("1. " mid-typing): the item regex
        // rejects it while the branch trigger accepts it, so nothing consumed
        // the line; without this the loop re-entered forever and hard-froze
        // the UI whenever the typewriter's slice ended at a list marker.
        blocks.push(
          <span className="md-paragraph" key={key}>
            <span className="md-paragraph-line">{line}</span>
          </span>
        );
        index += 1;
        blockIndex += 1;
        continue;
      }
      blocks.push(
        <span className="md-list" key={key}>
          {items.map((item, itemIndex) => (
            <span className="md-list-item" key={`${key}-item-${itemIndex}`}>
              <span className="md-bullet">{item.marker}</span>
              <span className="md-list-text">{renderInline(item.text, `${key}-item-${itemIndex}`)}</span>
            </span>
          ))}
        </span>
      );
      blockIndex += 1;
      continue;
    }

    const paragraph = [];
    while (index < lines.length && lines[index].trim() && !isSpecialMarkdownLine(lines[index])) {
      paragraph.push(lines[index]);
      index += 1;
    }
    if (!paragraph.length) {
      // Special-looking line ("# " with no text) that no branch consumed:
      // render it raw and move on, never leave index unadvanced.
      paragraph.push(line);
      index += 1;
    }
    blocks.push(
      <span className="md-paragraph" key={key}>
        {paragraph.map((item, lineIndex) => (
          <span className="md-paragraph-line" key={`${key}-line-${lineIndex}`}>
            {lineIndex > 0 && <br />}
            {renderInline(item, `${key}-line-${lineIndex}`)}
          </span>
        ))}
      </span>
    );
    blockIndex += 1;
  }

  return blocks;
}

// memo: markdown parsing is the most expensive part of a transcript render;
// re-parse only when the message content actually changes.
export const MessageText = memo(function MessageText({ content }) {
  const parsed = parseSources(collapsePastedText(content));
  return (
    <span className="message-content">
      <span className="line-text markdown-body">{renderMarkdown(parsed.body)}</span>
      {parsed.sources.length > 0 && (
        <span className="source-list">
          {parsed.sources.map((source) => (
            <a className="source-link" href={source.url} target="_blank" rel="noreferrer" title={source.url} key={`${source.title}-${source.url}`}>
              {faviconUrl(source.url) ? <img className="source-icon" src={faviconUrl(source.url)} alt="" /> : <span className="source-icon fallback">&gt;</span>}
              <span className="source-meta">
                <span className="source-title">{source.title}</span>
                <span className="source-domain">{sourceDomain(source.url)}</span>
              </span>
            </a>
          ))}
        </span>
      )}
    </span>
  );
});
