import { execFile } from "node:child_process";
import { mkdtemp, realpath, rm, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { isAbsolute, join, relative } from "node:path";

const CHANNEL = "openclaw-weixin";
const MATCH_LINK = /https?:\/\/(?:www\.|mobile\.)?(?:x\.com|twitter\.com)\/(?:[A-Za-z0-9_]+|i\/web)\/status\/[0-9]+/iu;
const TTL = 30 * 60 * 1000;
const HELP = "发一张 X 帖子截图，我会查原帖、发帖账号和帖里的说法。有原帖链接也可以一起发。";
const NO_RESULT = "这段会话里暂时没有可追问的核验结果，请先发一张 X 截图。";
const FAILURE = "这次核验没有完成。请重新发截图或原帖链接再试。";

function send(text) {
  return { handled: true, text };
}

function isInside(root, file) {
  const remainder = relative(root, file);
  return remainder !== "" && !isAbsolute(remainder) && !remainder.startsWith("..");
}

async function usableImage(path, mediaRoot) {
  if (typeof path !== "string" || !isAbsolute(path)) {
    return null;
  }
  try {
    const [root, file] = await Promise.all([realpath(mediaRoot), realpath(path)]);
    if (isInside(root, file) && (await stat(file)).isFile()) {
      return file;
    }
  } catch {
    // The attachment may have expired or failed to download.
  }
  return null;
}

function expire(map) {
  const now = Date.now();
  for (const [key, entry] of map) {
    if (entry.expires <= now) {
      map.delete(key);
    }
  }
}

function remember(map, key, value, limit) {
  map.delete(key);
  map.set(key, { value, expires: Date.now() + TTL });
  if (map.size > limit) {
    map.delete(map.keys().next().value);
  }
}

function runWorker(settings, input) {
  return new Promise((resolve, reject) => {
    const child = execFile(
      settings.python,
      ["-B", settings.worker, "--stdin"],
      { timeout: 100_000, maxBuffer: 256 * 1024, windowsHide: true, encoding: "utf8" },
      (error, stdout) => {
        if (error) {
          reject(error);
          return;
        }
        try {
          const result = JSON.parse(stdout);
          if (typeof result.reply !== "string" || !result.reply) {
            throw new Error("Missing reply");
          }
          resolve(result);
        } catch (parseError) {
          reject(parseError);
        }
      },
    );
    child.stdin.on("error", () => {});
    child.stdin.end(JSON.stringify(input), "utf8");
  });
}

async function startWorker(settings, input) {
  const directory = await mkdtemp(join(tmpdir(), "x-validator-previews-"));
  const dispose = () => rm(directory, { recursive: true, force: true }).catch(() => {});
  let sendingImages = false;
  try {
    const result = await runWorker(settings, { ...input, preview_dir: directory });
    const names = Array.isArray(result.candidate_images) ? result.candidate_images : [];
    const mediaUrls = [];
    for (const name of new Set(names.slice(0, 3))) {
      if (typeof name === "string" && /^candidate-[1-3]\.png$/u.test(name)) {
        const image = await usableImage(join(directory, name), directory);
        if (image) {
          mediaUrls.push(image);
        }
      }
    }
    sendingImages = mediaUrls.length > 0;
    return { result, mediaUrls, dispose: sendingImages ? dispose : undefined };
  } finally {
    if (!sendingImages) {
      await dispose();
    }
  }
}

export function makeHandler(settings, logger, run = startWorker) {
  const results = new Map();
  const seen = new Map();
  const active = new Map();

  return async function handle(event, context) {
    const channel = context.channelId ?? event.channelId;
    if (channel !== CHANNEL || event.isGroup || context.isGroup) {
      return;
    }
    const account = context.accountId ?? event.accountId ?? "";
    const sender = context.senderId ?? event.senderId ?? event.from;
    const conversation = context.sessionKey ?? event.sessionKey ?? context.conversationId ?? sender;
    if (!conversation) {
      return send("微信会话信息暂时不可用，请稍后重试。");
    }
    const session = JSON.stringify([account, sender ?? "", conversation]);
    const messageId = context.messageId ?? event.messageId;
    const key = messageId && JSON.stringify([session, messageId]);
    expire(seen);
    expire(results);
    if (key && seen.has(key)) {
      return { handled: true };
    }
    if (key) {
      remember(seen, key, true, 128);
    }

    const text = String(event.content ?? event.body ?? event.text ?? "").trim().slice(0, 4000);
    const media = Array.isArray(event.media) ? event.media : [];
    const pictures = media.filter((item) => item.kind === "image" || item.contentType?.startsWith("image/"));
    if (pictures.length > 1) {
      return send("请一次发一张截图，方便我把结果对应到原图。");
    }
    let imagePath = null;
    if (pictures.length === 1) {
      imagePath = await usableImage(pictures[0].path, settings.mediaRoot);
      if (!imagePath) {
        return send("这张截图暂时无法读取，请作为图片重新发送。");
      }
    } else if (event.mediaStagingPending || media.length) {
      return send("请把截图作为图片发送。");
    }
    if (settings.smokeTest && imagePath) {
      logger.info("X Validator received a WeChat screenshot");
      return send("已收到截图，微信收图和回复正常。");
    }

    const previousWork = active.get(session);
    if (previousWork?.count >= 3 || (!previousWork && active.size >= 4)) {
      return send("还有截图正在核验，请等结果返回后再试。");
    }
    const lane = previousWork ?? { tail: Promise.resolve(), count: 0 };
    active.set(session, lane);
    lane.count += 1;
    const work = lane.tail.then(async () => {
      expire(results);
      const previous = results.get(session)?.value;
      if (!imagePath && !MATCH_LINK.test(text)) {
        if (/证据|出处|来源|为什么|依据/u.test(text)) {
          return send(previous?.facts_text ?? NO_RESULT);
        }
        if (/原帖|原文|候选|链接/u.test(text)) {
          return send(previous?.original_text ?? NO_RESULT);
        }
        return send(HELP);
      }
      try {
        const { result, mediaUrls, dispose } = await run(settings, {
          image_path: imagePath, text, previous: imagePath ? null : previous,
        });
        remember(results, session, result, 32);
        logger.info("X Validator completed a WeChat verification");
        return { ...send(result.reply), mediaUrls, dispose };
      } catch {
        logger.warn("X Validator worker did not complete");
        return send(FAILURE);
      }
    });
    lane.tail = work.then(() => undefined, () => undefined);
    try {
      return await work;
    } finally {
      lane.count -= 1;
      if (lane.count === 0) {
        active.delete(session);
      }
    }
  };
}

export default {
  id: "x-validator-wechat",
  register(api) {
    const handle = makeHandler(api.pluginConfig, api.logger);
    api.on("reply_dispatch", async (event, context) => {
      const message = event.ctx;
      const paths = message.MediaPaths ?? (message.MediaPath ? [message.MediaPath] : []);
      const types = message.MediaTypes ?? (message.MediaType ? [message.MediaType] : []);
      const media = message.media ?? paths.map((path, index) => ({ path, contentType: types[index] }));
      const reply = await handle({
        channelId: event.originatingChannel ?? message.Provider ?? message.Surface,
        accountId: message.AccountId ?? event.originatingAccountId,
        sessionKey: message.SessionKey ?? event.sessionKey,
        senderId: message.SenderId ?? message.From,
        messageId: message.MessageSidFull ?? message.MessageSid,
        content: message.BodyForCommands ?? message.RawBody ?? message.Body,
        isGroup: (event.originatingChatType ?? message.ChatType) !== "direct",
        media,
        mediaStagingPending: message.mediaStagingPending,
      }, {});
      if (!reply?.handled) {
        return;
      }
      let queuedFinal = false;
      let queuedMedia = false;
      try {
        if (!context.abortSignal?.aborted) {
          for (const mediaUrl of reply.mediaUrls ?? []) {
            queuedMedia = context.dispatcher.sendFinalReply({ mediaUrl }) || queuedMedia;
          }
          queuedFinal = Boolean(reply.text && context.dispatcher.sendFinalReply({ text: reply.text }));
        }
      } finally {
        if (reply.dispose) {
          if (queuedMedia) {
            void context.dispatcher.waitForIdle().then(reply.dispose, reply.dispose);
          } else {
            await reply.dispose();
          }
        }
      }
      queuedFinal ||= queuedMedia;
      return { handled: true, queuedFinal, counts: context.dispatcher.getQueuedCounts() };
    }, { timeoutMs: 320_000 });
  },
};
