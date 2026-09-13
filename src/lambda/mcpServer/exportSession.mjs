/**
 * Export a completed crawl session's extracted text/metadata artifacts to
 * ContextWeave's raw ingestion prefix (see /home/user/ContextWeave CLAUDE.md
 * "Repository Signal Files" and src/preprocessor/handler.py).
 *
 * The crawler (src/crawler/crawl.py) persists, per session:
 *   states.json        structured per-state metadata (headings, links,
 *                       visible text preview, interactive elements)
 *   transitions.json   directed graph of page transitions
 *   trace.zip           Playwright trace (binary)
 *   screenshots/*.png   full-page screenshots (binary, one per state)
 *   crawler.log          EC2 UserData execution log
 *
 * ContextWeave's preprocessor dispatches by extension (.md/.markdown,
 * .yaml/.yml, .puml/.plantuml/.pu/.wsd, .pdf, .docx/.doc, else plain text)
 * and reads raw/<repo>/... from S3. We export only text/metadata that is
 * directly ingestible that way: the two JSON manifests as-is, plus a
 * synthesized Markdown summary (richer prose for embedding than raw JSON).
 * trace.zip and the screenshots are binary and are not exported.
 */
import { PutObjectCommand } from '@aws-sdk/client-s3';

export const CONTEXTWEAVE_RAW_PREFIX = 'raw/screenweave';

/** S3 keys (relative to the ContextWeave raw bucket root) this export writes. */
export function exportKeys(sessionId) {
  const base = `${CONTEXTWEAVE_RAW_PREFIX}/${sessionId}`;
  return {
    states: `${base}/states.json`,
    transitions: `${base}/transitions.json`,
    summary: `${base}/summary.md`,
  };
}

/** Render a human-readable Markdown summary of a session's captured states. */
export function buildSummaryMarkdown(sessionId, statesData) {
  const states = Array.isArray(statesData?.states) ? statesData.states : [];
  const lines = [
    `# ScreenWeave crawl session ${sessionId}`,
    '',
    `- Base URL: ${statesData?.base_url || 'unknown'}`,
    `- Captured at: ${statesData?.captured_at || 'unknown'}`,
    `- Total states: ${states.length}`,
    '',
  ];

  for (const s of states) {
    lines.push(`## ${s.state_id || 'state'} — ${s.title || s.url || ''}`.trim());
    lines.push('');
    lines.push(`- URL: ${s.url || ''}`);
    lines.push(`- Trigger: ${s.trigger_action || 'navigation'}${s.trigger_label ? ` (${s.trigger_label})` : ''}`);
    lines.push(`- Timestamp: ${s.timestamp || ''}`);
    if (Array.isArray(s.headings) && s.headings.length) {
      const headingText = s.headings.map((h) => h?.text).filter(Boolean).join(' | ');
      if (headingText) lines.push(`- Headings: ${headingText}`);
    }
    if (Array.isArray(s.interactive_elements) && s.interactive_elements.length) {
      lines.push(`- Interactive elements: ${s.interactive_elements.join(', ')}`);
    }
    if (s.visible_text_preview) {
      lines.push('', s.visible_text_preview.trim());
    }
    lines.push('');
  }

  return lines.join('\n');
}

/**
 * Upload a session's states.json / transitions.json (verbatim) plus a
 * generated summary.md to the ContextWeave raw bucket. Returns the keys
 * written. `s3Client` is any object exposing `.send()` (an S3Client, or a
 * stub in tests).
 */
export async function uploadSessionExport({ s3Client, bucket, sessionId, statesText, transitionsText }) {
  const keys = exportKeys(sessionId);

  let statesData = {};
  try {
    statesData = JSON.parse(statesText);
  } catch {
    // Malformed states.json still gets copied verbatim below; the summary
    // just falls back to placeholder values.
  }
  const summaryMd = buildSummaryMarkdown(sessionId, statesData);

  const puts = [
    { key: keys.states, body: statesText, contentType: 'application/json' },
    { key: keys.transitions, body: transitionsText, contentType: 'application/json' },
    { key: keys.summary, body: summaryMd, contentType: 'text/markdown' },
  ];

  for (const p of puts) {
    await s3Client.send(new PutObjectCommand({
      Bucket: bucket,
      Key: p.key,
      Body: p.body,
      ContentType: p.contentType,
    }));
  }

  return keys;
}
