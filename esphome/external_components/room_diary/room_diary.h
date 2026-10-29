#pragma once

#include "esphome/core/component.h"
#include "esphome/components/microphone/microphone.h"
#include "esphome/components/switch/switch.h"

#include <string>
#include <vector>
#include <atomic>

#ifdef USE_ESP32
#include <WiFi.h>
#endif

namespace esphome {
namespace room_diary {

class RoomDiary : public Component {
 public:
  void set_microphone(microphone::Microphone *mic) { this->mic_ = mic; }
  void set_host(const std::string &host) { this->host_ = host; }
  void set_port(uint16_t port) { this->port_ = port; }
  void set_rms_threshold(uint16_t thr) { this->rms_threshold_ = thr; }
  void set_enable_switch(switch_::Switch *sw) { this->enable_switch_ = sw; }

  void setup() override;
  void loop() override;
  float get_setup_priority() const override { return setup_priority::AFTER_WIFI; }

 protected:
  void on_audio_(const std::vector<uint8_t> &data);
  void ensure_connected_();
  void maybe_send_(const uint8_t *data, size_t len);
  uint32_t rms_i16_(const int16_t *samples, size_t n);

  microphone::Microphone *mic_{nullptr};
  switch_::Switch *enable_switch_{nullptr};
  std::string host_;
  uint16_t port_{10500};
  uint16_t rms_threshold_{400};
  std::atomic<bool> enabled_{true};
  uint32_t last_connect_try_ms_{0};
  std::vector<uint8_t> pending_;
  uint32_t sample_rate_{16000};

#ifdef USE_ESP32
  WiFiClient client_;
#endif
};

}  // namespace room_diary
}  // namespace esphome
