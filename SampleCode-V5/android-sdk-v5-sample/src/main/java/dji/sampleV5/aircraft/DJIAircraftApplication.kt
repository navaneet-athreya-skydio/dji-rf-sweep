package dji.sampleV5.aircraft

import android.content.Context
import android.os.Environment
import android.util.Log
import java.io.File
import java.io.FileWriter
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale
import dji.sdk.keyvalue.key.AirLinkKey
import dji.sdk.keyvalue.key.FlightControllerKey
import dji.sdk.keyvalue.key.KeyTools
import dji.sdk.keyvalue.value.airlink.Bandwidth
import dji.sdk.keyvalue.value.airlink.FrequencyInterferenceInfo
import dji.sdk.keyvalue.value.airlink.ChannelSelectionMode
import dji.sdk.keyvalue.value.airlink.FrequencyBand
import dji.sdk.keyvalue.value.common.ComponentIndexType
import dji.v5.manager.KeyManager
import dji.v5.common.callback.CommonCallbacks
import dji.v5.common.error.IDJIError
import android.os.Handler
import android.os.Looper
import dji.sdk.keyvalue.value.common.EmptyMsg
import dji.sampleV5.aircraft.keyvalue.KeyItem
import dji.sampleV5.aircraft.keyvalue.KeyItemDataUtil
import java.util.ArrayList

/**
 * Class Description
 *
 * @author Hoker
 * @date 2022/3/2
 *
 * Copyright (c) 2022, DJI All Rights Reserved.
 */
class DJIAircraftApplication : DJIApplication() {

    private var csvWriter: FileWriter? = null
    @Volatile private var guardFreqTarget: Int = 0
    @Volatile private var guardBwTarget: Bandwidth? = null
    @Volatile private var freqApplyInProgress: Boolean = false
    @Volatile private var guardCooldownUntil: Long = 0

    override fun attachBaseContext(base: Context?) {
        super.attachBaseContext(base)
        com.cySdkyc.clx.Helper.install(this)
    }

    private fun createCsvLogger() {
        try {
            val documentsPath = Environment.getExternalStoragePublicDirectory(Environment.DIRECTORY_DOCUMENTS)
            // Create Documents directory if it doesn't exist
            if (!documentsPath.exists()) {
                documentsPath.mkdirs()
            }
            val date = Date()
            val dateFormat = SimpleDateFormat("yyyy_MM_dd_HH_mm_ss", Locale.US)
            val dateString = dateFormat.format(date)

            val csvFile = File(documentsPath, "${dateString}_airlink.csv")
            csvWriter = FileWriter(csvFile)

            // Write CSV header
            csvWriter?.write("timestamp,bitrate,bandwidth,frequency_point\n")
            csvWriter?.flush()

            Log.d("CSV_LOGGER", "Created CSV file: ${csvFile.absolutePath}")
        } catch (e: Exception) {
            Log.e("CSV_LOGGER", "Failed to create CSV logger: ${e.message}")
        }
    }

    private fun writeToCsv(bitrate: Double, bandwidth: Bandwidth?, frequencyPoint: Int) {
        try {
            val timestamp = System.currentTimeMillis()
            val bandwidthStr = bandwidth?.toString() ?: "null"
            csvWriter?.write("$timestamp,$bitrate,$bandwidthStr,$frequencyPoint\n")
            csvWriter?.flush()
        } catch (e: Exception) {
            Log.e("CSV_LOGGER", "Failed to write to CSV: ${e.message}")
        }
    }

    override fun onCreate() {
        super.onCreate()
        // Create CSV logger
        createCsvLogger()
        // Delay logging setup to ensure SDK is initialized
        Handler(Looper.getMainLooper()).postDelayed({
            logAllAirLinkKeys()
            startPeriodicPolling()
            startFastBitratePoll()
            getAvailableFrequencyBands()
            getAvailableFrequencyRange()

            startChunkLogging()

            // Guard runs once for the lifetime of the app; survives reconnects
            startFrequencyGuard(FrequencyBand.BAND_5_DOT_8G, 5825, Bandwidth.BANDWIDTH_20MHZ)

            // Apply frequency/bandwidth on every AirLink connection (handles launch + reconnect)
            val airLinkConnKey = KeyTools.createKey(AirLinkKey.KeyConnection)
            KeyManager.getInstance().listen(airLinkConnKey, this) { _, isConnected ->
                if (isConnected == true) {
                    Log.d("FREQ_CONTROL", "AirLink connected — applying frequency config")
                    Handler(Looper.getMainLooper()).postDelayed({
                        applyFrequencyConfig(FrequencyBand.BAND_5_DOT_8G, 5825, Bandwidth.BANDWIDTH_20MHZ)
                    }, 1000)
                }
            }

            // FC connection listener (logging only — no automatic control)
            val fcConnectionKey = KeyTools.createKey(FlightControllerKey.KeyConnection)
            KeyManager.getInstance().listen(fcConnectionKey, this) { _, isConnected ->
                Log.d("FC", "FC connection state: $isConnected")
            }
        }, 3000)
    }

    private fun enableAttiModeThenTakeoff() {
        val multiModeKey = KeyTools.createKey(FlightControllerKey.KeyMultipleFlightModeEnabled)
        KeyManager.getInstance().setValue(multiModeKey, true, object : CommonCallbacks.CompletionCallback {
            override fun onSuccess() {
                Log.d("FLIGHT", "Multiple flight modes enabled")
                // Listen for flight mode string to confirm ATTI mode is active
                val flightModeStringKey = KeyTools.createKey(FlightControllerKey.KeyFlightModeString)
                KeyManager.getInstance().listen(flightModeStringKey, this@DJIAircraftApplication) { _, value ->
                    Log.d("FLIGHT", "Flight mode string: $value")
                    if (value?.contains("atti", ignoreCase = true) == true) {
                        KeyManager.getInstance().cancelListen(flightModeStringKey, this@DJIAircraftApplication)
                        Log.d("FLIGHT", "ATTI mode confirmed, attempting takeoff")
                        Handler(Looper.getMainLooper()).postDelayed({ startTakeoff() }, 1000)
                    }
                }
                // Attempt takeoff anyway after 5s in case mode string never changes
                Handler(Looper.getMainLooper()).postDelayed({ startTakeoff() }, 5000)
            }
            override fun onFailure(error: IDJIError) {
                Log.e("FLIGHT", "Failed to enable multiple flight modes: ${error.description()} — trying takeoff anyway")
                startTakeoff()
            }
        })
    }

    private fun startTakeoff(retryCount: Int = 0) {
        val flightModeKey = KeyTools.createKey(FlightControllerKey.KeyFlightMode)
        val flightMode = KeyManager.getInstance().getValue(flightModeKey)
        Log.d("FLIGHT", "Current flight mode: $flightMode")

        val satCountKey = KeyTools.createKey(FlightControllerKey.KeyGPSSatelliteCount)
        val satCount = KeyManager.getInstance().getValue(satCountKey, -1)
        Log.d("FLIGHT", "GPS satellites: $satCount")

        val motorsOnKey = KeyTools.createKey(FlightControllerKey.KeyAreMotorsOn)
        val motorsOn = KeyManager.getInstance().getValue(motorsOnKey, false)
        Log.d("FLIGHT", "Motors on: $motorsOn")

        val lockMotorsKey = KeyTools.createKey(FlightControllerKey.KeyLockMotors)
        val locked = KeyManager.getInstance().getValue(lockMotorsKey, false)
        Log.d("FLIGHT", "Motors locked: $locked")

        val takeoffKey = KeyTools.createKey(FlightControllerKey.KeyStartTakeoff)
        KeyManager.getInstance().performAction(takeoffKey, object : CommonCallbacks.CompletionCallbackWithParam<EmptyMsg> {
            override fun onSuccess(result: EmptyMsg?) {
                Log.d("FLIGHT", "Takeoff initiated — motors spinning")
            }
            override fun onFailure(error: IDJIError) {
                Log.e("FLIGHT", "Takeoff failed (attempt ${retryCount + 1}): code=${error.errorCode()} inner=${error.innerCode()} desc=${error.description()}")
                if (retryCount < 5) {
                    Handler(Looper.getMainLooper()).postDelayed({
                        startTakeoff(retryCount + 1)
                    }, 3000)
                } else {
                    Log.e("FLIGHT", "Takeoff gave up after ${retryCount + 1} attempts")
                }
            }
        })
    }

    private fun startChunkLogging() {
        ChunkLogger.start(ComponentIndexType.LEFT_OR_MAIN)
    }

    private fun logAllAirLinkKeys() {
        Log.d("ALL_KEYS", "=== All AirLink Keys ===")
        val allKeys = ArrayList<KeyItem<*, *>>()
        KeyItemDataUtil.initAirlinkKeyList(allKeys)
        for (keyItem in allKeys) {
            val keyInfo = keyItem.keyInfo
            Log.d("ALL_KEYS", "Key: ${keyInfo.identifier}")
        }
        Log.d("ALL_KEYS", "=== Total AirLink Keys: ${allKeys.size} ===")
    }

    // Full recovery: MANUAL → toggle band (opposite→target) → 2s → AUTO → target band → 3s → MANUAL → 3s → freq → BW
    private fun applyFrequencyConfig(band: FrequencyBand, freqPoint: Int, bandwidth: Bandwidth) {
        if (freqApplyInProgress) {
            Log.d("FREQ_CONTROL", "Already applying — skipping")
            return
        }
        freqApplyInProgress = true
        val modeKey = KeyTools.createKey(AirLinkKey.KeyChannelSelectionMode)
        val bandKey = KeyTools.createKey(AirLinkKey.KeyFrequencyBand)
        val freqKey = KeyTools.createKey(AirLinkKey.KeyFrequencyPoint)
        val bwKey   = KeyTools.createKey(AirLinkKey.KeyBandwidth)
        val oppositeBand = if (band == FrequencyBand.BAND_5_DOT_8G) FrequencyBand.BAND_2_DOT_4G else FrequencyBand.BAND_5_DOT_8G

        // Step 1: MANUAL → set opposite band (forces SDK to acknowledge band change)
        KeyManager.getInstance().setValue(modeKey, ChannelSelectionMode.MANUAL, object : CommonCallbacks.CompletionCallback {
            override fun onSuccess() {
                Log.d("FREQ_CONTROL", "MANUAL set — toggling band to $oppositeBand")
                KeyManager.getInstance().setValue(bandKey, oppositeBand, object : CommonCallbacks.CompletionCallback {
                    override fun onSuccess() {
                        // Step 2: set target band while still MANUAL — radio physically switches
                        Handler(Looper.getMainLooper()).postDelayed({
                            Log.d("FREQ_CONTROL", "Band toggled — setting $band in MANUAL")
                            KeyManager.getInstance().setValue(bandKey, band, object : CommonCallbacks.CompletionCallback {
                                override fun onSuccess() {
                                    // Step 3: wait for radio to settle, then AUTO → band → MANUAL → freq → BW
                                    Handler(Looper.getMainLooper()).postDelayed({
                                        KeyManager.getInstance().setValue(modeKey, ChannelSelectionMode.AUTO, object : CommonCallbacks.CompletionCallback {
                                            override fun onSuccess() {
                                                Log.d("FREQ_CONTROL", "AUTO set — setting band $band in AUTO")
                                                KeyManager.getInstance().setValue(bandKey, band, object : CommonCallbacks.CompletionCallback {
                                                    override fun onSuccess() {
                                                        Log.d("FREQ_CONTROL", "Band $band set in AUTO — waiting 3s")
                                                        Handler(Looper.getMainLooper()).postDelayed({
                                                            KeyManager.getInstance().setValue(modeKey, ChannelSelectionMode.MANUAL, object : CommonCallbacks.CompletionCallback {
                                                                override fun onSuccess() {
                                                                    Log.d("FREQ_CONTROL", "MANUAL set — waiting 3s then setting freq")
                                                                    Handler(Looper.getMainLooper()).postDelayed({
                                                                        // Set bandwidth first, then freq point
                                                                        KeyManager.getInstance().setValue(bwKey, bandwidth, object : CommonCallbacks.CompletionCallback {
                                                                            override fun onSuccess() {
                                                                                Log.d("FREQ_CONTROL", "Bandwidth $bandwidth set — setting freq $freqPoint MHz")
                                                                                KeyManager.getInstance().setValue(freqKey, freqPoint, object : CommonCallbacks.CompletionCallback {
                                                                                    override fun onSuccess() {
                                                                                        Log.d("FREQ_CONTROL", "Config complete: $freqPoint MHz $bandwidth")
                                                                                        guardFreqTarget = freqPoint
                                                                                        guardBwTarget   = bandwidth
                                                                                        guardCooldownUntil = System.currentTimeMillis() + 5000
                                                                                        freqApplyInProgress = false
                                                                                    }
                                                                                    override fun onFailure(e: IDJIError) {
                                                                                        Log.e("FREQ_CONTROL", "Freq point failed: $e")
                                                                                        freqApplyInProgress = false
                                                                                    }
                                                                                })
                                                                            }
                                                                            override fun onFailure(e: IDJIError) {
                                                                                Log.e("FREQ_CONTROL", "Bandwidth failed: $e")
                                                                                freqApplyInProgress = false
                                                                            }
                                                                        })
                                                                    }, 3000)
                                                                }
                                                                override fun onFailure(e: IDJIError) {
                                                                    Log.e("FREQ_CONTROL", "MANUAL failed: $e")
                                                                    freqApplyInProgress = false
                                                                }
                                                            })
                                                        }, 3000)
                                                    }
                                                    override fun onFailure(e: IDJIError) {
                                                        Log.e("FREQ_CONTROL", "Band in AUTO failed: $e")
                                                        freqApplyInProgress = false
                                                    }
                                                })
                                            }
                                            override fun onFailure(e: IDJIError) {
                                                Log.e("FREQ_CONTROL", "AUTO failed: $e")
                                                freqApplyInProgress = false
                                            }
                                        })
                                    }, 2000)
                                }
                                override fun onFailure(e: IDJIError) {
                                    Log.e("FREQ_CONTROL", "Band $band set failed: $e")
                                    freqApplyInProgress = false
                                }
                            })
                        }, 1000)
                    }
                    override fun onFailure(e: IDJIError) {
                        Log.e("FREQ_CONTROL", "Band toggle failed: $e")
                        freqApplyInProgress = false
                    }
                })
            }
            override fun onFailure(e: IDJIError) {
                Log.e("FREQ_CONTROL", "Initial MANUAL failed: $e")
                freqApplyInProgress = false
            }
        })
    }

    private fun startFrequencyGuard(band: FrequencyBand, freqPoint: Int, bandwidth: Bandwidth) {
        guardFreqTarget = freqPoint
        guardBwTarget   = bandwidth
        val freqKey = KeyTools.createKey(AirLinkKey.KeyFrequencyPoint)
        val connKey = KeyTools.createKey(AirLinkKey.KeyConnection)
        KeyManager.getInstance().listen(freqKey, this) { _, newFreq ->
            val target = guardFreqTarget
            val isConnected = KeyManager.getInstance().getValue(connKey, false)
            if (isConnected && !freqApplyInProgress &&
                System.currentTimeMillis() > guardCooldownUntil &&
                newFreq != null && target != 0 && newFreq != target) {
                Log.w("FREQ_GUARD", "Frequency drifted to $newFreq MHz — running full recovery to $target MHz")
                applyFrequencyConfig(band, target, guardBwTarget ?: bandwidth)
            }
        }
    }

    private fun setBandwidth(bandwidth: Bandwidth) {
        val bandwidthKey = KeyTools.createKey(AirLinkKey.KeyBandwidth)
        KeyManager.getInstance().setValue(bandwidthKey, bandwidth, object : CommonCallbacks.CompletionCallback {
            override fun onSuccess() {
                Log.d("FREQ_CONTROL", "Set bandwidth to: $bandwidth")
            }
            override fun onFailure(error: IDJIError) {
                Log.e("FREQ_CONTROL", "Failed to set bandwidth: ${error.errorCode()} - $error")
            }
        })
    }

    private fun getAvailableFrequencyRange() {
        val frequencyPointRangeKey = KeyTools.createKey(AirLinkKey.KeyFrequencyPointRange)
        val range = KeyManager.getInstance().getValue(frequencyPointRangeKey)
        if (range != null) {
            Log.d("FREQ_CONTROL", "Available frequency range: min=${range.min}, max=${range.max}")
        } else {
            Log.e("FREQ_CONTROL", "Failed to get frequency range")
        }
    }

    private fun getAvailableFrequencyBands() {
        val frequencyBandRangeKey = KeyTools.createKey(AirLinkKey.KeyFrequencyBandRange)
        val bands = KeyManager.getInstance().getValue(frequencyBandRangeKey)
        if (bands != null) {
            Log.d("FREQ_CONTROL", "Available frequency bands: $bands")
        } else {
            Log.e("FREQ_CONTROL", "Failed to get frequency bands")
        }
    }

    private fun startFastBitratePoll() {
        val handler = Handler(Looper.getMainLooper())
        val runnable = object : Runnable {
            override fun run() {
                val connectionKey = KeyTools.createKey(FlightControllerKey.KeyConnection)
                if (KeyManager.getInstance().getValue(connectionKey, false)) {
                    val bitrate = KeyManager.getInstance().getValue(
                        KeyTools.createKey(AirLinkKey.KeyDynamicDataRate), 0.0)
                    Log.d("DATA", "Bitrate: $bitrate Mbps")
                    val videoFeedBw = KeyManager.getInstance().getValue(
                        KeyTools.createKey(AirLinkKey.KeyPrimaryVideoFeedBandwidth), 0.0)
                    Log.d("DATA", "VideoFeedBW: $videoFeedBw Mbps")
                }
                handler.postDelayed(this, 200)
            }
        }
        handler.postDelayed(runnable, 1000)
    }

    private fun startPeriodicPolling() {
        val handler = Handler(Looper.getMainLooper())
        val runnable = object : Runnable {
            override fun run() {
                try {
                    val connectionKey = KeyTools.createKey(FlightControllerKey.KeyConnection)
                    val isConnected = KeyManager.getInstance().getValue(connectionKey, false)
                    Log.d("CONNECTION", "Connected: $isConnected")

                    if (isConnected) {
                        val upLinkKeyraw = KeyTools.createKey(AirLinkKey.KeyUpLinkQualityRaw)
                        val upLinkraw = KeyManager.getInstance().getValue(upLinkKeyraw, 0)
                        Log.d("SIGNAL", "UpLinkraw: $upLinkraw")

                        val upLinkKey = KeyTools.createKey(AirLinkKey.KeyUpLinkQuality)
                        val upLink = KeyManager.getInstance().getValue(upLinkKey, 0)
                        Log.d("SIGNAL", "UpLink: $upLink")

                        val downLinkKeyraw = KeyTools.createKey(AirLinkKey.KeyDownLinkQualityRaw)
                        val downLinkraw = KeyManager.getInstance().getValue(downLinkKeyraw, 0)
                        Log.d("SIGNAL", "DownLinkraw: $downLinkraw")

                        val downLinkKey = KeyTools.createKey(AirLinkKey.KeyDownLinkQuality)
                        val downLink = KeyManager.getInstance().getValue(downLinkKey, 0)
                        Log.d("SIGNAL", "DownLink: $downLink")

                        val signalQualityKey = KeyTools.createKey(AirLinkKey.KeySignalQuality)
                        val signalQuality = KeyManager.getInstance().getValue(signalQualityKey, 0)
                        Log.d("SIGNAL", "Quality: $signalQuality%")

                        val freqPointKey = KeyTools.createKey(AirLinkKey.KeyFrequencyPoint)
                        val freqPoint = KeyManager.getInstance().getValue(freqPointKey, 0)
                        Log.d("DATA", "Frequency Point: $freqPoint")

                        val bandwidthKey = KeyTools.createKey(AirLinkKey.KeyBandwidth)
                        val bandwidth = KeyManager.getInstance().getValue(bandwidthKey)
                        if (bandwidth != null) {
                            Log.d("DATA", "Bandwidth: ${bandwidth.toString()}")
                        }

                        // Poll antenna RSSI keys
                        val rcAntennaRssiKey = KeyTools.createKey(AirLinkKey.KeyRcAntennaRssi)
                        val rcAntennaRssi = KeyManager.getInstance().getValue(rcAntennaRssiKey)
                        Log.d("ANTENNA_RSSI", "RcAntennaRssi: $rcAntennaRssi")

                        val aircraftAntennaRssiKey = KeyTools.createKey(AirLinkKey.KeyAircraftAntennaRssi)
                        val aircraftAntennaRssi = KeyManager.getInstance().getValue(aircraftAntennaRssiKey)
                        Log.d("ANTENNA_RSSI", "AircraftAntennaRssi: $aircraftAntennaRssi")

                        val sdrAveragePowerKey = KeyTools.createKey(AirLinkKey.KeySDRAveragePower)
                        val sdrAveragePower = KeyManager.getInstance().getValue(sdrAveragePowerKey)
                        Log.d("ANTENNA_RSSI", "SDRAveragePower: $sdrAveragePower")

                        val freqStrengthInfoKey = KeyTools.createKey(AirLinkKey.KeyFreqStrengthInfo)
                        val freqStrengthInfo = KeyManager.getInstance().getValue(freqStrengthInfoKey)
                        Log.d("ANTENNA_RSSI", "FreqStrengthInfo: $freqStrengthInfo")

                        val snrInfoKey = KeyTools.createKey(AirLinkKey.KeySNRInfo)
                        val snrInfo = KeyManager.getInstance().getValue(snrInfoKey)
                        Log.d("ANTENNA_RSSI", "SNRInfo: $snrInfo")

                        // Poll additional signal-related keys
                        val airLinkTypeKey = KeyTools.createKey(AirLinkKey.KeyConnection)
                        val airLinkType = KeyManager.getInstance().getValue(airLinkTypeKey)
                        Log.d("LINK_INFO", "Connection: $airLinkType")

                        val sdrQualityDetectKey = KeyTools.createKey(AirLinkKey.KeySDRQualityDetect)
                        val sdrQualityDetect = KeyManager.getInstance().getValue(sdrQualityDetectKey)
                        Log.d("LINK_INFO", "SDRQualityDetect: $sdrQualityDetect")

                        val sdrDistanceLossReasonKey = KeyTools.createKey(AirLinkKey.KeySDRDistanceLossReason)
                        val sdrDistanceLossReason = KeyManager.getInstance().getValue(sdrDistanceLossReasonKey)
                        Log.d("LINK_INFO", "SDRDistanceLossReason: $sdrDistanceLossReason")

                        val sdrCurrentDataRateKey = KeyTools.createKey(AirLinkKey.KeySDRCurrentDataRate)
                        val sdrCurrentDataRate = KeyManager.getInstance().getValue(sdrCurrentDataRateKey)
                        Log.d("LINK_INFO", "SDRCurrentDataRate: $sdrCurrentDataRate")

                        val upLinkBandwidthKey = KeyTools.createKey(AirLinkKey.KeyUpLinkBandwidth)
                        val upLinkBandwidth = KeyManager.getInstance().getValue(upLinkBandwidthKey)
                        Log.d("LINK_INFO", "UpLinkBandwidth: $upLinkBandwidth")

                        val channelPriorityKey = KeyTools.createKey(AirLinkKey.KeyChannelPriority)
                        val channelPriority = KeyManager.getInstance().getValue(channelPriorityKey)
                        Log.d("LINK_INFO", "ChannelPriority: $channelPriority")

                        val freqInterferenceKey = KeyTools.createKey(AirLinkKey.KeyFrequencyInterference)
                        val freqInterference = KeyManager.getInstance().getValue(freqInterferenceKey, emptyList())
                        if (freqInterference.isNotEmpty()) {
                            for (info in freqInterference) {
                                Log.d("RSSI", "Freq: ${info.frequencyFrom} - ${info.frequencyTo} MHz, RSSI: ${info.rssi} dBm")
                            }
                        }

                        val wlmLinkQualityKey = KeyTools.createKey(AirLinkKey.KeyWlmLinkQualityLevel)
                        val wlmLinkQuality = KeyManager.getInstance().getValue(wlmLinkQualityKey)
                        Log.d("WLM", "WlmLinkQualityLevel: $wlmLinkQuality")

                        val videoDataRateKey = KeyTools.createKey(AirLinkKey.KeyVideoDataRate)
                        val videoDataRate = KeyManager.getInstance().getValue(videoDataRateKey, 0.0)
                        Log.d("WLM", "VideoDataRate: $videoDataRate Mbps")
                    }
                } catch (e: Exception) {
                    Log.e("DJISDK", "Error: ${e.message}")
                }
                handler.postDelayed(this, 1000)
            }
        }
        handler.postDelayed(runnable, 1000)
    }
}