import assert from "node:assert/strict";
import { mkdtemp, readFile, rmdir, unlink, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";
import plugin, { makeHandler } from "../wechat-plugin/index.js";

const logger = { info() {}, warn() {} };
const dm = { channelId: "openclaw-weixin", accountId: "paired", sessionKey: "private", senderId: "owner" };

test("a finalized WeChat message uses the attached media and original reply dispatcher", async () => {
  const mediaRoot = await mkdtemp(join(tmpdir(), "x-validator-test-"));
  const path = join(mediaRoot, "attachment.jpg");
  try {
    await writeFile(path, "placeholder");
    let hook;
    plugin.register({
      logger, pluginConfig: { mediaRoot, smokeTest: true },
      on(name, handler) {
        assert.equal(name, "reply_dispatch");
        hook = handler;
      },
    });
    const texts = [];
    const dispatch = {
      dispatcher: {
        sendFinalReply({ text }) { texts.push(text); return true; },
        getQueuedCounts() { return { final: texts.length }; },
      },
    };
    const message = {
      Provider: "openclaw-weixin", ChatType: "direct", SessionKey: "private",
      From: "owner", MessageSid: "photo-1", MediaPath: path, MediaType: "image/*",
    };
    assert.equal((await hook({ ctx: message }, dispatch)).queuedFinal, true);
    assert.equal((await hook({ ctx: message }, dispatch)).queuedFinal, false);
    assert.deepEqual(texts, ["已收到截图，微信收图和回复正常。"]);
    assert.equal(await hook({ ctx: { ...message, ChatType: "group" } }, dispatch), undefined);
  } finally {
    await unlink(path);
    await rmdir(mediaRoot);
  }
});

test("link replies and evidence questions use only the preceding result from that chat", async () => {
  const run = async (settings, request) => {
    assert.match(request.text, /x\.com\/example\/status\/42/u);
    return { result: { reply: "result", facts_text: "evidence", original_text: "original" } };
  };
  const handle = makeHandler({}, logger, run);
  const link = { content: "https://x.com/example/status/42", messageId: "1" };
  assert.equal((await handle(link, dm)).text, "result");
  assert.equal((await handle({ content: "证据", messageId: "2" }, dm)).text, "evidence");
  const other = { ...dm, sessionKey: "other" };
  assert.doesNotMatch((await handle({ content: "证据", messageId: "2" }, other)).text, /evidence/u);
});

test("local status records API attempts without copying a chat message", async () => {
  const monitorDir = await mkdtemp(join(tmpdir(), "x-validator-status-"));
  const file = join(monitorDir, "last-check.json");
  try {
    const handle = makeHandler({ monitorDir }, logger, async (settings, request) => {
      if (request.text.endsWith("/43")) throw new Error("fixture timeout");
      return { result: { reply: "private fixture reply", request_counts: { x: 2, model: 3, web: 1 } } };
    });
    await handle({ content: "https://x.com/example/status/42", messageId: "1" }, dm);
    const successful = JSON.parse(await readFile(file, "utf8"));
    assert.deepEqual(successful.counts, { x: 2, model: 3, web: 1 });
    assert.equal(successful.ok, true);
    assert.ok(successful.duration_ms >= 0);
    assert.doesNotMatch(JSON.stringify(successful), /private fixture|example\/status|owner/u);
    await handle({ content: "https://x.com/example/status/43", messageId: "2" }, dm);
    const failed = JSON.parse(await readFile(file, "utf8"));
    assert.equal(failed.ok, false);
    assert.equal(failed.counts, null);
  } finally {
    await unlink(file);
    await rmdir(monitorDir);
  }
});

test("candidate screenshots precede the reply text and stay until delivery settles", async () => {
  const directory = await mkdtemp(join(tmpdir(), "x-validator-test-"));
  const worker = join(directory, "worker.py");
  try {
    await writeFile(worker, `
import json, sys
from pathlib import Path
folder = Path(json.load(sys.stdin)["preview_dir"])
names = ["candidate-1.png", "candidate-2.png"]
for name in names:
    (folder / name).write_bytes(b"fixture")
print(json.dumps({"reply": "candidate results", "candidate_images": names + ["../foreign.png"]}))
`);
    let hook;
    plugin.register({
      logger, pluginConfig: { python: "python", worker },
      on(name, handler) { hook = handler; },
    });
    const replies = [];
    const { promise, resolve } = Promise.withResolvers();
    const dispatch = {
      dispatcher: {
        sendFinalReply(payload) { replies.push(payload); return true; },
        getQueuedCounts() { return { final: replies.length }; },
        waitForIdle() { return promise; },
      },
    };
    const message = {
      Provider: "openclaw-weixin", ChatType: "direct", SessionKey: "private",
      From: "owner", MessageSid: "candidate-1", Body: "https://x.com/example/status/42",
    };
    assert.equal((await hook({ ctx: message }, dispatch)).queuedFinal, true);
    assert.deepEqual(replies.at(-1), { text: "candidate results" });
    assert.equal(replies.length, 3);
    for (const { mediaUrl } of replies.slice(0, -1)) {
      assert.equal(await readFile(mediaUrl, "utf8"), "fixture");
    }
    resolve();
    await assert.rejects(async () => {
      for (let index = 0; index < 30; index += 1) {
        await new Promise((done) => setTimeout(done, 10));
        await readFile(replies[0].mediaUrl);
      }
    }, { code: "ENOENT" });
  } finally {
    await unlink(worker);
    await rmdir(directory);
  }
});
