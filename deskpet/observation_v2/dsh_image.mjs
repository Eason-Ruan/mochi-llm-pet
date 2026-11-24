import { randomUUID } from 'node:crypto'

export const name = 'mochi-observation-image'
export const inject = ['tools']

export function apply(ctx, {width, height, maxBytes}) {
  // This profile owns a single capture and has no session persistence or filesystem tools.
  let capture
  ctx.provide('attachments', {
    async saveImages(images) {
      const image = images[0]
      if (capture || images.length !== 1 || image.mediaType !== 'image/jpeg'
          || image.data.length > maxBytes || image.data[0] !== 0xff || image.data[1] !== 0xd8) {
        throw new Error('Unexpected Observation image')
      }
      const ref = {
        attachmentId: 'sha256:' + randomUUID().replaceAll('-', '').repeat(2),
        mediaType: image.mediaType, bytes: image.data.length, width, height,
      }
      capture = {ref, data: image.data}
      return [ref]
    },
    async readImageRequest(ref, target, signal) {
      signal?.throwIfAborted()
      if (!capture || ref.attachmentId !== capture.ref.attachmentId
          || target.width < width || target.height < height || target.maxBytes < capture.data.length) {
        throw new Error('Observation image is outside its request budget')
      }
      return {
        variantId: ref.attachmentId, attachment: capture.ref, data: capture.data,
        mediaType: ref.mediaType, bytes: capture.data.length, width, height,
        depth: 'uchar', space: 'srgb', hasAlpha: false,
      }
    },
    imageHostPath() { return undefined },
  })
  ctx.provide('workingDirectory', {
    async ensure(agent) { return agent.session.header.cwd ?? process.cwd() },
    async set() { throw new Error('Observation does not change working directory') },
  })
  ctx.tools.guard(() => 'Observation has no tool capabilities')
  ctx.effect(() => () => { capture = undefined })
}
