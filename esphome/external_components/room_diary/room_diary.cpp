#include "room_diary.h"
#include "esphome/core/log.h"
#include <cmath>

namespace esphome {
namespace room_diary {

static const char *const TAG = "room_diary";

void RoomDiary::setup() {
  if (this->mic_ == nullptr) {
    ESP_LOGE(TAG, "No microphone configured");
    this->mark_failed();
    return;
  }
  this->mic_->add_data_callback([this](const std::vector<uint8_t> &data) { this->on_audio_(data); });
  ESP_LOGI(TAG, "Room diary target %s:%u", this->host_.c_str(), this->port_);
}

uint32_t RoomDiary::rms_i16_(const int16_t *samples, size_t n) {
  if (n == 0)
    return 0;
  uint64_t acc = 0;
  for (size_t i = 0; i < n; i++) {
    int32_t v = samples[i];
    acc += (uint64_t) (v * v);
  }
  return (uint32_t) sqrtf((float) acc / (float) n);
}

void RoomDiary::ensure_connected_() {
#ifdef USE_ESP32
  if (this->client_.connected())
    return;
  uint32_t now = millis();
  if (now - this->last_connect_try_ms_ < 3000)
    return;
  this->last_connect_try_ms_ = now;
  this->client_.stop();
  if (this->client_.connect(this->host_.c_str(), this->port_)) {
    ESP_LOGI(TAG, "PCM connected");
  } else {
    ESP_LOGW(TAG, "PCM connect failed");
  }
#endif
}

void RoomDiary::maybe_send_(const uint8_t *data, size_t len) {
#ifdef USE_ESP32
  if (!this->client_.connected() || len == 0 || len > 64000)
    return;
  uint8_t hdr[12];
  hdr[0] = 'H';
  hdr[1] = 'V';
  hdr[2] = '0';
  hdr[3] = '1';
  uint32_t rate = this->sample_rate_;
  hdr[4] = (rate >> 24) & 0xff;
  hdr[5] = (rate >> 16) & 0xff;
  hdr[6] = (rate >> 8) & 0xff;
  hdr[7] = rate & 0xff;
  uint32_t n = (uint32_t) len;
  hdr[8] = (n >> 24) & 0xff;
  hdr[9] = (n >> 16) & 0xff;
  hdr[10] = (n >> 8) & 0xff;
  hdr[11] = n & 0xff;
  if (this->client_.write(hdr, sizeof(hdr)) != sizeof(hdr)) {
    this->client_.stop();
    return;
  }
  size_t wrote = this->client_.write(data, len);
  if (wrote != len)
    this->client_.stop();
#endif
}

void RoomDiary::on_audio_(const std::vector<uint8_t> &data) {
  if (!this->enabled_.load())
    return;
  if (data.size() < 4)
    return;
  const auto *samples = reinterpret_cast<const int16_t *>(data.data());
  size_t n = data.size() / 2;
  if (this->rms_i16_(samples, n) < this->rms_threshold_)
    return;
  // copy to pending; actual TCP write on main loop
  if (this->pending_.size() + data.size() > 32000)
    this->pending_.clear();
  this->pending_.insert(this->pending_.end(), data.begin(), data.end());
}

void RoomDiary::loop() {
  bool want = true;
  if (this->enable_switch_ != nullptr)
    want = this->enable_switch_->state;
  this->enabled_.store(want);

  if (!want) {
#ifdef USE_ESP32
    if (this->client_.connected())
      this->client_.stop();
#endif
    this->pending_.clear();
    return;
  }

  this->ensure_connected_();
  if (!this->pending_.empty()) {
    std::vector<uint8_t> chunk;
    chunk.swap(this->pending_);
    this->maybe_send_(chunk.data(), chunk.size());
  }
}

}  // namespace room_diary
}  // namespace esphome
