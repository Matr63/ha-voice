#pragma once

#include "esphome/core/automation.h"
#include "esphome/core/component.h"
#include "esphome/components/microphone/microphone.h"
#include "esphome/components/ring_buffer/ring_buffer.h"
#ifdef USE_SPEAKER
#include "esphome/components/speaker/speaker.h"
#endif

#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include <atomic>
#include <cstdint>
#include <memory>
#include <mutex>
#include <string>
#include <vector>

namespace esphome {
namespace ha_voice_sat {

// Wire protocol (both directions), big endian, 16-byte header:
//   0..3  magic "HV02"
//   4     type
//   5     channels
//   6     bits per sample
//   7     reserved
//   8..11 sample rate
//   12..15 payload bytes
enum FrameType : uint8_t {
  FRAME_AUDIO = 0x01,      // up: mic PCM, down: TTS PCM
  FRAME_HELLO = 0x02,      // up: "key=value\n" device info
  FRAME_EVENT = 0x03,      // up: text event (tts_done, button:2, listen:on)
  FRAME_PING = 0x04,       // both: keepalive
  FRAME_TTS_START = 0x10,  // down: rate/channels in header
  FRAME_TTS_END = 0x11,    // down: no more TTS audio
  FRAME_STATE = 0x12,      // down: 1 byte, see SatState
  FRAME_VOLUME = 0x13,     // down: 1 byte 0..100
  FRAME_TTS_ABORT = 0x14,  // down: drop queued TTS now
  FRAME_SET = 0x15,        // down: "key=value" board setting (mic_gain, led_brightness, listen)
};

enum SatState : uint8_t {
  STATE_IDLE = 0,
  STATE_WAKE = 1,
  STATE_LISTENING = 2,
  STATE_THINKING = 3,
  STATE_REPLYING = 4,
  STATE_ERROR = 5,
  STATE_SLEEP = 6,
};

static const size_t HEADER_SIZE = 16;
static const size_t MAX_PAYLOAD = 256 * 1024;

class HaVoiceSat : public Component {
 public:
  void set_microphone(microphone::Microphone *mic) { this->mic_ = mic; }
#ifdef USE_SPEAKER
  void set_speaker(speaker::Speaker *spk) { this->speaker_ = spk; }
#endif
  void set_host(const std::string &host);
  void set_port(uint16_t port) { this->port_ = port; }
  void set_device_name(const std::string &name) { this->device_name_ = name; }
  void set_board(const std::string &board) { this->board_ = board; }
  void set_listening(bool on);
  bool is_listening() const { return this->listening_.load(); }
  bool is_connected() const { return this->connected_.load(); }
  void send_event(const std::string &event);

  Trigger<int> *get_state_trigger() { return &this->state_trigger_; }
  Trigger<float> *get_volume_trigger() { return &this->volume_trigger_; }
  Trigger<std::string, std::string> *get_setting_trigger() { return &this->setting_trigger_; }
  Trigger<> *get_tts_start_trigger() { return &this->tts_start_trigger_; }
  Trigger<> *get_tts_end_trigger() { return &this->tts_end_trigger_; }
  Trigger<> *get_connected_trigger() { return &this->connected_trigger_; }
  Trigger<> *get_disconnected_trigger() { return &this->disconnected_trigger_; }

  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::AFTER_WIFI; }

 protected:
  static void net_task_(void *arg);
  void net_run_();
  bool connect_();
  void disconnect_();
  bool send_frame_(uint8_t type, const uint8_t *data, size_t len, uint32_t rate = 0, uint8_t channels = 0,
                   uint8_t bits = 0);
  void send_hello_();
  void poll_recv_();
  void handle_frame_();
  void on_audio_(const std::vector<uint8_t> &data);
  void loop_speaker_();
  static void drain_(ring_buffer::RingBuffer *ring);

  microphone::Microphone *mic_{nullptr};
#ifdef USE_SPEAKER
  speaker::Speaker *speaker_{nullptr};
#endif
  std::mutex host_mutex_;
  std::string host_;
  uint16_t port_{10500};
  std::string device_name_;
  std::string board_{"waveshare"};
  // 32-bit mics (XVF3800) are sent as 16-bit: half the Wi-Fi traffic
  std::vector<uint8_t> pack_buf_;

  std::atomic<bool> listening_{true};
  std::atomic<bool> connected_{false};
  bool was_connected_{false};
  std::atomic<bool> host_changed_{false};

  // mic -> net task
  std::unique_ptr<ring_buffer::RingBuffer> tx_ring_;
  // net task -> speaker (main loop)
  std::unique_ptr<ring_buffer::RingBuffer> rx_ring_;
  std::mutex events_mutex_;
  std::vector<std::string> events_;
  std::mutex settings_mutex_;
  std::vector<std::string> settings_;

  TaskHandle_t net_task_handle_{nullptr};
  int fd_{-1};
  uint32_t last_connect_try_ms_{0};
  uint32_t last_tx_ms_{0};
  uint32_t last_rx_ms_{0};

  // receive state machine (net task only)
  uint8_t rx_hdr_[HEADER_SIZE];
  size_t rx_hdr_got_{0};
  uint8_t rx_type_{0};
  uint32_t rx_rate_{0};
  uint8_t rx_channels_{0};
  uint32_t rx_left_{0};
  std::vector<uint8_t> rx_small_;

  // net task -> main loop
  std::atomic<int> pending_state_{-1};
  std::atomic<int> pending_volume_{-1};
  std::atomic<bool> tts_start_pending_{false};
  std::atomic<bool> tts_end_pending_{false};
  std::atomic<bool> tts_abort_pending_{false};
  std::atomic<uint32_t> tts_rate_{16000};
  std::atomic<uint8_t> tts_channels_{1};

  // speaker side (main loop only)
  enum class TtsPhase { IDLE, PLAYING, DRAINING };
  TtsPhase tts_phase_{TtsPhase::IDLE};
  std::vector<uint8_t> play_buf_;
  size_t play_off_{0};

  Trigger<int> state_trigger_;
  Trigger<float> volume_trigger_;
  Trigger<std::string, std::string> setting_trigger_;
  Trigger<> tts_start_trigger_;
  Trigger<> tts_end_trigger_;
  Trigger<> connected_trigger_;
  Trigger<> disconnected_trigger_;
};

template<typename... Ts> class SetListeningAction : public Action<Ts...>, public Parented<HaVoiceSat> {
 public:
  TEMPLATABLE_VALUE(bool, listening)
  void play(Ts... x) override { this->parent_->set_listening(this->listening_.value(x...)); }
};

template<typename... Ts> class SendEventAction : public Action<Ts...>, public Parented<HaVoiceSat> {
 public:
  TEMPLATABLE_VALUE(std::string, event)
  void play(Ts... x) override { this->parent_->send_event(this->event_.value(x...)); }
};

template<typename... Ts> class IsConnectedCondition : public Condition<Ts...>, public Parented<HaVoiceSat> {
 public:
  bool check(Ts... x) override { return this->parent_->is_connected(); }
};

}  // namespace ha_voice_sat
}  // namespace esphome
