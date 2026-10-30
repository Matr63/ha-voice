#include "room_diary.h"

#include "esphome/core/hal.h"
#include "esphome/core/log.h"

#include <arpa/inet.h>
#include <cerrno>
#include <cmath>
#include <cstring>
#include <netdb.h>
#include <sys/socket.h>
#include <unistd.h>

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

void RoomDiary::disconnect_() {
  if (this->fd_ < 0)
    return;
  ::shutdown(this->fd_, SHUT_RDWR);
  ::close(this->fd_);
  this->fd_ = -1;
}

void RoomDiary::ensure_connected_() {
  if (this->connected_())
    return;
  uint32_t now = millis();
  if (now - this->last_connect_try_ms_ < 3000)
    return;
  this->last_connect_try_ms_ = now;

  char port_str[8];
  snprintf(port_str, sizeof(port_str), "%u", this->port_);

  struct addrinfo hints = {};
  hints.ai_family = AF_UNSPEC;
  hints.ai_socktype = SOCK_STREAM;
  struct addrinfo *res = nullptr;
  int err = getaddrinfo(this->host_.c_str(), port_str, &hints, &res);
  if (err != 0 || res == nullptr) {
    ESP_LOGW(TAG, "PCM DNS failed for %s", this->host_.c_str());
    return;
  }

  int fd = ::socket(res->ai_family, res->ai_socktype, res->ai_protocol);
  if (fd < 0) {
    freeaddrinfo(res);
    ESP_LOGW(TAG, "PCM socket create failed");
    return;
  }

  struct timeval tv;
  tv.tv_sec = 3;
  tv.tv_usec = 0;
  setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
  setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));

  if (::connect(fd, res->ai_addr, res->ai_addrlen) != 0) {
    ESP_LOGW(TAG, "PCM connect failed: %s", strerror(errno));
    ::close(fd);
    freeaddrinfo(res);
    return;
  }
  freeaddrinfo(res);
  this->fd_ = fd;
  ESP_LOGI(TAG, "PCM connected");
}

void RoomDiary::maybe_send_(const uint8_t *data, size_t len) {
  if (!this->connected_() || len == 0 || len > 64000)
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

  ssize_t wrote = ::send(this->fd_, hdr, sizeof(hdr), 0);
  if (wrote != (ssize_t) sizeof(hdr)) {
    this->disconnect_();
    return;
  }
  wrote = ::send(this->fd_, data, len, 0);
  if (wrote != (ssize_t) len)
    this->disconnect_();
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
    this->disconnect_();
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
