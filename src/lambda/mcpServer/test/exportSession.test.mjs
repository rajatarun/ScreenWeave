import { test } from 'node:test';
import assert from 'node:assert/strict';
import { exportKeys, buildSummaryMarkdown, uploadSessionExport } from '../exportSession.mjs';

test('exportKeys places artifacts under raw/screenweave/<session_id>/', () => {
  const keys = exportKeys('sess-abc123');
  assert.deepEqual(keys, {
    states: 'raw/screenweave/sess-abc123/states.json',
    transitions: 'raw/screenweave/sess-abc123/transitions.json',
    summary: 'raw/screenweave/sess-abc123/summary.md',
  });
});

test('buildSummaryMarkdown renders each state as a Markdown section', () => {
  const md = buildSummaryMarkdown('sess-abc123', {
    base_url: 'https://example.com',
    captured_at: '2026-01-01T00:00:00Z',
    states: [
      {
        state_id: 'state_0001',
        url: 'https://example.com/',
        title: 'Home',
        trigger_action: 'navigation',
        timestamp: '2026-01-01T00:00:01Z',
        headings: [{ tag: 'h1', text: 'Welcome' }],
        interactive_elements: ['Sign up'],
        visible_text_preview: 'Welcome to the site.',
      },
    ],
  });

  assert.match(md, /# ScreenWeave crawl session sess-abc123/);
  assert.match(md, /Base URL: https:\/\/example\.com/);
  assert.match(md, /## state_0001 — Home/);
  assert.match(md, /Headings: Welcome/);
  assert.match(md, /Interactive elements: Sign up/);
  assert.match(md, /Welcome to the site\./);
});

test('uploadSessionExport writes exactly states.json, transitions.json, and summary.md', async () => {
  const puts = [];
  const s3Client = {
    send: async (command) => {
      puts.push(command.input);
      return {};
    },
  };

  const statesText = JSON.stringify({
    session_id: 'sess-abc123',
    base_url: 'https://example.com',
    states: [{ state_id: 'state_0001', url: 'https://example.com/', title: 'Home' }],
  });
  const transitionsText = JSON.stringify({ session_id: 'sess-abc123', transitions: [] });

  const keys = await uploadSessionExport({
    s3Client,
    bucket: 'contextweave-raw-bucket',
    sessionId: 'sess-abc123',
    statesText,
    transitionsText,
  });

  assert.equal(puts.length, 3);

  const byKey = Object.fromEntries(puts.map((p) => [p.Key, p]));
  assert.equal(byKey[keys.states].Bucket, 'contextweave-raw-bucket');
  assert.equal(byKey[keys.states].Body, statesText);
  assert.equal(byKey[keys.states].ContentType, 'application/json');

  assert.equal(byKey[keys.transitions].Body, transitionsText);
  assert.equal(byKey[keys.transitions].ContentType, 'application/json');

  assert.equal(byKey[keys.summary].ContentType, 'text/markdown');
  assert.match(byKey[keys.summary].Body, /# ScreenWeave crawl session sess-abc123/);
});

test('uploadSessionExport tolerates malformed states.json without throwing', async () => {
  const puts = [];
  const s3Client = { send: async (command) => { puts.push(command.input); return {}; } };

  const keys = await uploadSessionExport({
    s3Client,
    bucket: 'contextweave-raw-bucket',
    sessionId: 'sess-broken',
    statesText: '{not valid json',
    transitionsText: '{}',
  });

  assert.equal(puts.length, 3);
  const summaryPut = puts.find((p) => p.Key === keys.summary);
  assert.match(summaryPut.Body, /# ScreenWeave crawl session sess-broken/);
  assert.match(summaryPut.Body, /Total states: 0/);
});
