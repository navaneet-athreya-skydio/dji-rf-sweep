package dji.sampleV5.aircraft

import android.os.Handler
import android.os.Looper
import android.util.Log
import dji.sdk.keyvalue.value.common.ComponentIndexType
import dji.v5.manager.datacenter.MediaDataCenter
import dji.v5.manager.interfaces.ICameraStreamManager

object ChunkLogger {
    private var listener: ICameraStreamManager.ReceiveStreamListener? = null
    private val handler = Handler(Looper.getMainLooper())
    private var activeIndex: ComponentIndexType? = null

    @Volatile private var chunkCount = 0
    @Volatile private var totalBytes = 0L
    @Volatile private var minBytes = Int.MAX_VALUE
    @Volatile private var maxBytes = 0
    @Volatile private var keyframeCount = 0
    @Volatile private var droppedFrames = 0
    @Volatile private var consecutiveEmptySeconds = 0

    @Volatile private var lastPts = -1L
    @Volatile private var lastWidth = 0
    @Volatile private var lastHeight = 0
    @Volatile private var lastFps = 0
    @Volatile private var lastMime: ICameraStreamManager.MimeType? = null

    private val summaryRunnable = object : Runnable {
        override fun run() {
            val count = chunkCount
            val total = totalBytes
            val mn = minBytes
            val mx = maxBytes
            val kf = keyframeCount
            val dropped = droppedFrames
            val w = lastWidth
            val h = lastHeight
            val fps = lastFps

            chunkCount = 0
            totalBytes = 0L
            minBytes = Int.MAX_VALUE
            maxBytes = 0
            keyframeCount = 0
            droppedFrames = 0

            if (count > 0) {
                consecutiveEmptySeconds = 0
                val avg = total / count
                Log.d("CHUNK", "chunks=$count totalBytes=$total avg=${avg}B min=${mn}B max=${mx}B keyframes=$kf dropped=$dropped fps=$fps res=${w}x${h} mime=${lastMime?.name}")
            } else {
                consecutiveEmptySeconds++
                Log.d("CHUNK", "chunks=0 (no stream data, empty=${consecutiveEmptySeconds}s)")
                // reattach listener if stream went silent (link drop + recovery)
                if (consecutiveEmptySeconds >= 2) {
                    reattach()
                }
            }
            handler.postDelayed(this, 1000)
        }
    }

    private fun reattach() {
        val idx = activeIndex ?: return
        val l = listener ?: return
        try {
            MediaDataCenter.getInstance().cameraStreamManager.removeReceiveStreamListener(l)
            MediaDataCenter.getInstance().cameraStreamManager.addReceiveStreamListener(idx, l)
            Log.d("CHUNK", "Stream listener reattached after ${consecutiveEmptySeconds}s silence")
        } catch (e: Exception) {
            Log.e("CHUNK", "Reattach failed: ${e.message}")
        }
    }

    fun start(cameraIndex: ComponentIndexType) {
        activeIndex = cameraIndex
        listener = ICameraStreamManager.ReceiveStreamListener { _, _, length, info ->
            chunkCount++
            totalBytes += length
            if (length < minBytes) minBytes = length
            if (length > maxBytes) maxBytes = length
            if (info.isKeyFrame) keyframeCount++

            // detect dropped frames via PTS gaps
            val pts = info.presentationTimeMs
            if (lastPts >= 0 && info.frameRate > 0) {
                val frameDurationMs = 1000.0 / info.frameRate
                val gap = pts - lastPts
                if (gap > frameDurationMs * 1.5) {
                    droppedFrames += ((gap / frameDurationMs) - 1).toInt()
                }
            }
            lastPts = pts

            if (info.mimeType != null && info.mimeType != lastMime) {
                Log.d("CHUNK", "MIME CHANGED: $lastMime → ${info.mimeType?.name}")
                lastMime = info.mimeType
            }
            if (info.width != lastWidth || info.height != lastHeight) {
                Log.d("CHUNK", "RESOLUTION CHANGED: ${lastWidth}x${lastHeight} → ${info.width}x${info.height}")
                lastWidth = info.width
                lastHeight = info.height
            }
            if (info.frameRate != lastFps) {
                Log.d("CHUNK", "FPS CHANGED: $lastFps → ${info.frameRate}")
                lastFps = info.frameRate
            }
        }
        MediaDataCenter.getInstance().cameraStreamManager.addReceiveStreamListener(cameraIndex, listener!!)
        handler.postDelayed(summaryRunnable, 1000)
        Log.d("CHUNK", "ChunkLogger started for $cameraIndex")
    }

    fun stop() {
        handler.removeCallbacks(summaryRunnable)
        lastPts = -1L
        consecutiveEmptySeconds = 0
        activeIndex = null
        listener?.let {
            MediaDataCenter.getInstance().cameraStreamManager.removeReceiveStreamListener(it)
            listener = null
        }
    }
}
