import AVFoundation
import Foundation
import UIKit

/// Native capture for the web Ask panel. The web view owns the gesture and
/// presentation; this object owns the microphone, the temporary recording,
/// and the authenticated upload to the Mac mini.
final class VoiceCapture: NSObject, AVAudioRecorderDelegate {
    typealias Event = [String: Any]

    var onEvent: ((Event) -> Void)?

    private var recorder: AVAudioRecorder?
    private var recordingURL: URL?
    private var limitTimer: Timer?
    private var sessionID = ""
    private var holdActive = false
    private var uploading = false
    private var uploadTask: Task<Void, Never>?
    private var uploadGeneration = 0
    private var permissionGeneration = 0

    func begin(sessionID: String) {
        guard recorder == nil, !uploading else {
            emit(state: "error", message: "Voice input is already busy.")
            return
        }
        self.sessionID = sessionID
        holdActive = true
        permissionGeneration += 1
        let generation = permissionGeneration
        emit(state: "starting")

        AVAudioApplication.requestRecordPermission { [weak self] granted in
            DispatchQueue.main.async {
                guard let self, generation == self.permissionGeneration else { return }
                guard granted else {
                    self.holdActive = false
                    self.emit(state: "error", message:
                        "Microphone access is off. Enable it for avctl in Settings.")
                    return
                }
                // The permission sheet can outlive the original press. Never
                // begin recording after the finger has already gone away.
                guard self.holdActive else {
                    self.emit(state: "cancelled")
                    return
                }
                self.startRecorder()
            }
        }
    }

    func finish(send: Bool) {
        holdActive = false
        guard !uploading else { return }
        guard let recorder, let url = recordingURL else {
            // Permission may still be resolving; invalidate its callback.
            permissionGeneration += 1
            emit(state: "cancelled")
            return
        }

        let duration = recorder.currentTime
        self.recorder = nil
        recorder.stop()
        recordingURL = nil
        limitTimer?.invalidate()
        limitTimer = nil
        deactivateAudioSession()

        guard send else {
            try? FileManager.default.removeItem(at: url)
            emit(state: "cancelled")
            return
        }
        guard duration >= 0.35 else {
            try? FileManager.default.removeItem(at: url)
            emit(state: "error", message: "Hold a little longer so I can hear you.")
            return
        }

        UIImpactFeedbackGenerator(style: .soft).impactOccurred()
        uploading = true
        emit(state: "transcribing")
        upload(url: url, sessionID: sessionID)
    }

    func cancel() {
        permissionGeneration += 1
        if uploading {
            uploadGeneration += 1
            uploadTask?.cancel()
            uploadTask = nil
            uploading = false
            emit(
                state: "error",
                message: "Voice request cancelled. An action may already have started; check before retrying.")
            return
        }
        finish(send: false)
    }

    private func startRecorder() {
        var candidateURL: URL?
        do {
            let audioSession = AVAudioSession.sharedInstance()
            try audioSession.setCategory(.record, mode: .measurement)
            try audioSession.setPreferredSampleRate(16_000)
            try audioSession.setActive(true)

            let url = FileManager.default.temporaryDirectory
                .appendingPathComponent("avctl-voice-\(UUID().uuidString)")
                .appendingPathExtension("wav")
            candidateURL = url
            let settings: [String: Any] = [
                AVFormatIDKey: Int(kAudioFormatLinearPCM),
                AVSampleRateKey: 16_000.0,
                AVNumberOfChannelsKey: 1,
                AVLinearPCMBitDepthKey: 16,
                AVLinearPCMIsBigEndianKey: false,
                AVLinearPCMIsFloatKey: false,
            ]
            let recorder = try AVAudioRecorder(url: url, settings: settings)
            recorder.delegate = self
            guard recorder.prepareToRecord(), recorder.record() else {
                throw VoiceCaptureError.couldNotRecord
            }
            self.recorder = recorder
            recordingURL = url
            UIImpactFeedbackGenerator(style: .medium).impactOccurred()
            emit(state: "listening")

            let timer = Timer(timeInterval: 30,
                              repeats: false) { [weak self] _ in
                self?.finish(send: true)
            }
            // A finger held inside WKWebView can keep the main run loop in a
            // tracking mode. A default-mode timer pauses there and can let the
            // WAV exceed the server's hard duration limit before pointer-up.
            RunLoop.main.add(timer, forMode: .common)
            limitTimer = timer
        } catch {
            holdActive = false
            if let candidateURL { try? FileManager.default.removeItem(at: candidateURL) }
            deactivateAudioSession()
            emit(state: "error", message: "The microphone could not start.")
        }
    }

    private func upload(url: URL, sessionID: String) {
        uploadGeneration += 1
        let generation = uploadGeneration
        // Keep the owner alive until this bounded request finishes. cancel()
        // still tears the task down immediately, and the immutable capture
        // avoids Swift 6's weak-capture/MainActor race diagnostic.
        let owner = self
        uploadTask = Task {
            defer { try? FileManager.default.removeItem(at: url) }
            do {
                let audio = try Data(contentsOf: url)
                guard var request = AvctlClient.request(
                    path: "/api/agent/voice", timeout: 300,
                    query: ["session": sessionID,
                            "request_id": UUID().uuidString])
                else { throw URLError(.badURL) }
                request.httpMethod = "POST"
                request.setValue("audio/wav", forHTTPHeaderField: "Content-Type")
                request.setValue("application/json", forHTTPHeaderField: "Accept")
                let (data, response) = try await URLSession.shared.upload(
                    for: request, from: audio)
                try Task.checkCancellation()
                guard let http = response as? HTTPURLResponse else {
                    throw URLError(.badServerResponse)
                }
                let status = http.statusCode
                let object = try JSONSerialization.jsonObject(with: data)
                let payload = object as? [String: Any] ?? [:]
                guard (200..<300).contains(status) else {
                    throw VoiceCaptureError.server(
                        payload["detail"] as? String ?? "The voice request failed.",
                        transcript: payload["transcript"] as? String)
                }
                guard let transcript = payload["transcript"] as? String,
                      !transcript.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty,
                      let message = payload["message"] as? String,
                      !message.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty
                else { throw VoiceCaptureError.invalidResponse }
                await MainActor.run {
                    guard generation == owner.uploadGeneration else { return }
                    owner.uploading = false
                    owner.uploadTask = nil
                    var event = payload
                    event["state"] = "result"
                    owner.onEvent?(event)
                    UINotificationFeedbackGenerator().notificationOccurred(.success)
                }
            } catch {
                if error is CancellationError ||
                    (error as? URLError)?.code == .cancelled {
                    // Explicit cancel() increments the generation and already
                    // reset the UI. A same-generation cancellation came from
                    // URLSession/the OS; without this branch the owner stayed
                    // uploading forever and hold-to-talk never re-enabled.
                    await MainActor.run {
                        guard generation == owner.uploadGeneration else { return }
                        owner.uploading = false
                        owner.uploadTask = nil
                        owner.emit(
                            state: "error",
                            message: "The voice request lost its reply. The action status is unknown, so check before retrying.")
                    }
                    return
                }
                let message: String
                let recoveredTranscript: String?
                if case VoiceCaptureError.server(let detail, let transcript) = error {
                    message = detail
                    recoveredTranscript = transcript
                } else if (error as? URLError)?.code == .timedOut {
                    message = "The voice request timed out. The action status is unknown, so check before retrying."
                    recoveredTranscript = nil
                } else if let code = (error as? URLError)?.code,
                          [.cannotFindHost, .cannotConnectToHost,
                           .notConnectedToInternet, .dnsLookupFailed].contains(code) {
                    message = "The Mac mini could not be reached."
                    recoveredTranscript = nil
                } else {
                    message = "The voice reply was lost or invalid. The action status is unknown, so check before retrying."
                    recoveredTranscript = nil
                }
                await MainActor.run {
                    guard generation == owner.uploadGeneration else { return }
                    owner.uploading = false
                    owner.uploadTask = nil
                    owner.emit(state: "error", message: message,
                               transcript: recoveredTranscript)
                    UINotificationFeedbackGenerator().notificationOccurred(.error)
                }
            }
        }
    }

    func audioRecorderEncodeErrorDidOccur(_ recorder: AVAudioRecorder,
                                          error: Error?) {
        guard self.recorder === recorder else { return }
        abandonRecording(message: "The microphone stopped recording.")
    }

    func audioRecorderDidFinishRecording(_ recorder: AVAudioRecorder,
                                         successfully flag: Bool) {
        // finish(send:) clears self.recorder before its intentional stop, so
        // reaching here with the same recorder always means an interruption
        // or an encoder-side early finish, even when AVFoundation says the
        // partial file itself is structurally valid.
        guard self.recorder === recorder else { return }
        abandonRecording(message: "Recording was interrupted. Hold to try again.")
    }

    private func abandonRecording(message: String) {
        self.recorder = nil
        if let url = recordingURL { try? FileManager.default.removeItem(at: url) }
        recordingURL = nil
        limitTimer?.invalidate()
        limitTimer = nil
        holdActive = false
        deactivateAudioSession()
        emit(state: "error", message: message)
    }

    private func deactivateAudioSession() {
        try? AVAudioSession.sharedInstance().setActive(
            false, options: .notifyOthersOnDeactivation)
    }

    private func emit(state: String, message: String? = nil,
                      transcript: String? = nil) {
        var event: Event = ["state": state]
        if let message { event["message"] = message }
        if let transcript, !transcript.isEmpty { event["transcript"] = transcript }
        onEvent?(event)
    }
}

private enum VoiceCaptureError: Error {
    case couldNotRecord
    case invalidResponse
    case server(String, transcript: String?)
}
