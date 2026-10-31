#include "ha_voice_sat.h"

#include "esphome/core/alloc_helpers.h"
#include "esphome/core/application.h"
#include "esphome/core/hal.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"
#include "esphome/core/version.h"
#include "esphome/components/audio/audio.h"
#include "esphome/components/network/util.h"

#include <cerrno>
#include <cstring>
#include <lwip/netdb.h>
#include <lwip/sockets.h>

namespace esphome {
namespace ha_voice_sat {

static const char *const TAG = "ha_voice_sat";

static const size_t TX_RING_SIZE = 64 * 1024;   // ~2 s of 16 kHz mono
static const size_t RX_RING_SIZE = 256 * 1024;  // ~5 s of 24 kHz mono TTS
static const size_t TX_CHUNK = 2048;            // 64 ms at 16 kHz mono
static const uint32_t PING_MS = 2000;
static const uint32_t RX_TIMEOUT_MS = 15000;    // server pings every 5 s
static const uint32_t RECONNECT_MS = 3000;

static void put_u32(uint8_t *p, uint32_t v) {
  p[0] = (v >> 24) & 0xff;
  p[1] = (v >> 16) & 0xff;
  p[2] = (v >> 8) & 0xff;
  p[3] = v & 0xff;
}

static uint32_t get_u32(const uint8_t *p) {
  return ((uint32_t) p[0] << 24) | ((uint32_t) p[1] << 16) | ((uint32_t) p[2] << 8) | (uint32_t) p[3];
}

static HighFrequencyLoopRequester high_freq;

void HaVoiceSat::drain_(ring_buffer::RingBuffer *ring) {
  size_t len = 0;
  while (ring->available() > 0) {
    void *item = ring->receive_acquire(len, ring->available(), 0);
    if (item == nullptr)
      break;
    ring->receive_release(item);
  }
}

void HaVoiceSat::set_host(const std::string &host) {
  std::lock_guard<std::mutex> lock(this->host_mutex_);
  if (host == this->host_)
    return;
  this->host_ = host;
  this->host_changed_.store(true);
}

void HaVoiceSat::set_listening(bool on) {
  if (this->listening_.exchange(on) != on) {
    ESP_LOGI(TAG, "Listening %s", on ? "ON" : "OFF");
    this->send_event(on ? "listen:on" : "listen:off");
  }
}

void HaVoiceSat::send_event(const std::string &event) {
  std::lock_guard<std::mutex> lock(this->events_mutex_);
  if (this->events_.size() < 16)
    this->events_.push_back(event);
}

void HaVoiceSat::setup() {
  if (this->mic_ == nullptr) {
    ESP_LOGE(TAG, "No microphone configured");
    this->mark_failed();
    return;
  }
  this->tx_ring_ = ring_buffer::RingBuffer::create(TX_RING_SIZE);
  this->rx_ring_ = ring_buffer::RingBuffer::create(RX_RING_SIZE);
  if (!this->tx_ring_ || !this->rx_ring_) {
    ESP_LOGE(TAG, "Failed to allocate audio buffers");
    this->mark_failed();
    return;
  }
  this->mic_->add_data_callback([this](const std::vector<uint8_t> &data) { this->on_audio_(data); });

  if (xTaskCreate(HaVoiceSat::net_task_, "ha_voice_net", 6144, this, 5, &this->net_task_handle_) != pdPASS) {
    ESP_LOGE(TAG, "Failed to start network task");
    this->mark_failed();
  }
}

void HaVoiceSat::dump_config() {
  ESP_LOGCONFIG(TAG, "HA Voice satellite:");
  {
    std::lock_guard<std::mutex> lock(this->host_mutex_);
    ESP_LOGCONFIG(TAG, "  Server: %s:%u", this->host_.c_str(), this->port_);
  }
  ESP_LOGCONFIG(TAG, "  Listening: %s", YESNO(this->listening_.load()));
#ifdef USE_SPEAKER
  ESP_LOGCONFIG(TAG, "  Speaker: %s", YESNO(this->speaker_ != nullptr));
#endif
}

// Mic task: no RMS gate here, voice activity is decided on the server.
void HaVoiceSat::on_audio_(const std::vector<uint8_t> &data) {
  if (!this->listening_.load() || !this->connected_.load() || data.empty())
    return;
  this->tx_ring_->write_without_replacement(data.data(), data.size(), 0);
}

void HaVoiceSat::loop() {
  bool c = this->connected_.load();
  if (c != this->was_connected_) {
    this->was_connected_ = c;
    if (c) {
      this->connected_trigger_.trigger();
    } else {
      this->disconnected_trigger_.trigger();
    }
  }

  // The satellite owns the microphone: nothing else (no wake word on the chip)
  // starts it, so the stream to the server never depends on another component.
  bool want_mic = this->listening_.load() && c;
  if (want_mic && this->mic_->is_stopped()) {
    this->mic_->start();
  } else if (!want_mic && this->mic_->is_running()) {
    this->mic_->stop();
    drain_(this->tx_ring_.get());
  }

  int s = this->pending_state_.exchange(-1);
  if (s >= 0)
    this->state_trigger_.trigger(s);
  int v = this->pending_volume_.exchange(-1);
  if (v >= 0)
    this->volume_trigger_.trigger(clamp(v, 0, 100) / 100.0f);

  std::vector<std::string> settings;
  {
    std::lock_guard<std::mutex> lock(this->settings_mutex_);
    settings.swap(this->settings_);
  }
  for (auto &kv : settings) {
    size_t eq = kv.find('=');
    if (eq == std::string::npos)
      continue;
    ESP_LOGI(TAG, "Setting %s", kv.c_str());
    this->setting_trigger_.trigger(kv.substr(0, eq), kv.substr(eq + 1));
  }

  this->loop_speaker_();
}

void HaVoiceSat::loop_speaker_() {
#ifdef USE_SPEAKER
  if (this->speaker_ != nullptr) {
    if (this->tts_abort_pending_.exchange(false)) {
      if (this->tts_phase_ != TtsPhase::IDLE) {
        this->speaker_->stop();
        high_freq.stop();
        this->tts_phase_ = TtsPhase::DRAINING;
      }
      this->play_buf_.clear();
      this->play_off_ = 0;
      this->tts_start_pending_.store(false);
      this->tts_end_pending_.store(false);
      drain_(this->rx_ring_.get());
    }

    if (this->tts_phase_ == TtsPhase::IDLE && this->tts_start_pending_.exchange(false)) {
      audio::AudioStreamInfo info(16, this->tts_channels_.load(), this->tts_rate_.load());
      this->speaker_->set_audio_stream_info(info);
      this->speaker_->start();
      this->play_buf_.clear();
      this->play_off_ = 0;
      this->tts_phase_ = TtsPhase::PLAYING;
      high_freq.start();
      this->tts_start_trigger_.trigger();
    }

    if (this->tts_phase_ == TtsPhase::PLAYING) {
      const size_t frame = 2 * std::max<uint8_t>(1, this->tts_channels_.load());
      for (int i = 0; i < 8; i++) {
        if (this->play_off_ >= this->play_buf_.size()) {
          this->play_buf_.resize(4096);
          size_t n = this->rx_ring_->read(this->play_buf_.data(), this->play_buf_.size(), 0);
          size_t rem = n % frame;
          if (rem != 0) {
            n += this->rx_ring_->read(this->play_buf_.data() + n, frame - rem, pdMS_TO_TICKS(5));
            n -= n % frame;
          }
          this->play_buf_.resize(n);
          this->play_off_ = 0;
          if (n == 0)
            break;
        }
        size_t w = this->speaker_->play(this->play_buf_.data() + this->play_off_,
                                        this->play_buf_.size() - this->play_off_, 0);
        this->play_off_ += w;
        if (w == 0)
          break;
      }
      if (this->tts_end_pending_.load() && this->play_off_ >= this->play_buf_.size() &&
          this->rx_ring_->available() == 0) {
        this->tts_end_pending_.store(false);
        this->speaker_->finish();
        high_freq.stop();
        this->tts_phase_ = TtsPhase::DRAINING;
      }
    }

    if (this->tts_phase_ == TtsPhase::DRAINING && this->speaker_->is_stopped()) {
      this->tts_phase_ = TtsPhase::IDLE;
      this->tts_end_trigger_.trigger();
      this->send_event("tts_done");
    }

    if (this->tts_phase_ == TtsPhase::IDLE && !this->tts_start_pending_.load() &&
        this->tts_end_pending_.exchange(false)) {
      this->send_event("tts_done");  // END without audio
    }
    return;
  }
#endif
  // No speaker: drop TTS but still acknowledge it so the server does not wait.
  this->tts_start_pending_.store(false);
  this->tts_abort_pending_.store(false);
  if (this->rx_ring_->available() > 0)
    drain_(this->rx_ring_.get());
  if (this->tts_end_pending_.exchange(false))
    this->send_event("tts_done");
}

// ---------------------------------------------------------------- net task

void HaVoiceSat::net_task_(void *arg) {
  static_cast<HaVoiceSat *>(arg)->net_run_();
  vTaskDelete(nullptr);
}

bool HaVoiceSat::connect_() {
  std::string host;
  {
    std::lock_guard<std::mutex> lock(this->host_mutex_);
    host = this->host_;
  }
  if (host.empty())
    return false;

  char port_str[8];
  snprintf(port_str, sizeof(port_str), "%u", this->port_);
  struct addrinfo hints = {};
  hints.ai_family = AF_INET;
  hints.ai_socktype = SOCK_STREAM;
  struct addrinfo *res = nullptr;
  if (getaddrinfo(host.c_str(), port_str, &hints, &res) != 0 || res == nullptr) {
    ESP_LOGW(TAG, "DNS failed for %s", host.c_str());
    return false;
  }
  int fd = ::socket(res->ai_family, res->ai_socktype, res->ai_protocol);
  if (fd < 0) {
    freeaddrinfo(res);
    ESP_LOGW(TAG, "Socket create failed");
    return false;
  }
  struct timeval tv = {.tv_sec = 2, .tv_usec = 0};
  setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
  int one = 1;
  setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
  setsockopt(fd, SOL_SOCKET, SO_KEEPALIVE, &one, sizeof(one));

  if (::connect(fd, res->ai_addr, res->ai_addrlen) != 0) {
    ESP_LOGW(TAG, "Connect to %s:%u failed: %s", host.c_str(), this->port_, strerror(errno));
    ::close(fd);
    freeaddrinfo(res);
    return false;
  }
  freeaddrinfo(res);
  this->fd_ = fd;
  this->rx_hdr_got_ = 0;
  this->rx_left_ = 0;
  this->last_rx_ms_ = millis();
  this->last_tx_ms_ = millis();
  drain_(this->tx_ring_.get());
  this->connected_.store(true);
  ESP_LOGI(TAG, "Connected to HA Voice %s:%u", host.c_str(), this->port_);
  return true;
}

void HaVoiceSat::disconnect_() {
  if (this->fd_ >= 0) {
    ::shutdown(this->fd_, SHUT_RDWR);
    ::close(this->fd_);
    this->fd_ = -1;
    ESP_LOGW(TAG, "Disconnected from HA Voice");
  }
  if (this->connected_.exchange(false)) {
    this->tts_abort_pending_.store(true);
  }
}

bool HaVoiceSat::send_frame_(uint8_t type, const uint8_t *data, size_t len, uint32_t rate, uint8_t channels,
                             uint8_t bits) {
  if (this->fd_ < 0)
    return false;
  uint8_t hdr[HEADER_SIZE];
  memcpy(hdr, "HV02", 4);
  hdr[4] = type;
  hdr[5] = channels;
  hdr[6] = bits;
  hdr[7] = 0;
  put_u32(hdr + 8, rate);
  put_u32(hdr + 12, (uint32_t) len);

  const uint8_t *parts[2] = {hdr, data};
  size_t sizes[2] = {HEADER_SIZE, len};
  for (int p = 0; p < 2; p++) {
    size_t off = 0;
    while (off < sizes[p]) {
      ssize_t w = ::send(this->fd_, parts[p] + off, sizes[p] - off, 0);
      if (w <= 0) {
        ESP_LOGW(TAG, "Send failed: %s", strerror(errno));
        this->disconnect_();
        return false;
      }
      off += (size_t) w;
    }
  }
  this->last_tx_ms_ = millis();
  return true;
}

void HaVoiceSat::send_hello_() {
  std::string ip;
  char ip_buf[network::IP_ADDRESS_BUFFER_SIZE];
  for (auto &addr : network::get_ip_addresses()) {
    if (addr.is_set() && addr.is_ip4()) {
      ip = addr.str_to(ip_buf);
      break;
    }
  }
  auto info = this->mic_->get_audio_stream_info();
  std::string name = this->device_name_.empty() ? App.get_name().str() : this->device_name_;
  std::string hello = "name=" + name + "\n" + "friendly_name=" + App.get_friendly_name().str() + "\n" +
                      "mac=" + get_mac_address_pretty() + "\n" + "ip=" + ip + "\n" +
                      "esphome=" ESPHOME_VERSION "\n" + "proto=2\n" +
                      "mic_rate=" + std::to_string(info.get_sample_rate()) + "\n" +
                      "mic_channels=" + std::to_string(info.get_channels()) + "\n" +
                      "listening=" + (this->listening_.load() ? "1" : "0") + "\n";
#ifdef USE_SPEAKER
  hello += std::string("speaker=") + (this->speaker_ != nullptr ? "1" : "0") + "\n";
#endif
  this->send_frame_(FRAME_HELLO, reinterpret_cast<const uint8_t *>(hello.data()), hello.size());
}

void HaVoiceSat::handle_frame_() {
  switch (this->rx_type_) {
    case FRAME_TTS_START:
      this->tts_rate_.store(this->rx_rate_ ? this->rx_rate_ : 16000);
      this->tts_channels_.store(this->rx_channels_ ? this->rx_channels_ : 1);
      this->tts_start_pending_.store(true);
      break;
    case FRAME_TTS_END:
      this->tts_end_pending_.store(true);
      break;
    case FRAME_TTS_ABORT:
      this->tts_abort_pending_.store(true);
      break;
    case FRAME_STATE:
      if (!this->rx_small_.empty())
        this->pending_state_.store(this->rx_small_[0]);
      break;
    case FRAME_VOLUME:
      if (!this->rx_small_.empty())
        this->pending_volume_.store(this->rx_small_[0]);
      break;
    case FRAME_SET: {
      std::lock_guard<std::mutex> lock(this->settings_mutex_);
      if (this->settings_.size() < 16)
        this->settings_.emplace_back(this->rx_small_.begin(), this->rx_small_.end());
      break;
    }
    default:
      break;
  }
}

void HaVoiceSat::poll_recv_() {
  uint8_t tmp[1024];
  while (this->fd_ >= 0) {
    if (this->rx_hdr_got_ < HEADER_SIZE) {
      ssize_t r = ::recv(this->fd_, this->rx_hdr_ + this->rx_hdr_got_, HEADER_SIZE - this->rx_hdr_got_, MSG_DONTWAIT);
      if (r == 0) {
        this->disconnect_();
        return;
      }
      if (r < 0) {
        if (errno != EAGAIN && errno != EWOULDBLOCK)
          this->disconnect_();
        return;
      }
      this->last_rx_ms_ = millis();
      this->rx_hdr_got_ += (size_t) r;
      if (this->rx_hdr_got_ < HEADER_SIZE)
        continue;
      if (memcmp(this->rx_hdr_, "HV02", 4) != 0) {
        ESP_LOGW(TAG, "Bad frame magic from server");
        this->disconnect_();
        return;
      }
      this->rx_type_ = this->rx_hdr_[4];
      this->rx_channels_ = this->rx_hdr_[5];
      this->rx_rate_ = get_u32(this->rx_hdr_ + 8);
      this->rx_left_ = get_u32(this->rx_hdr_ + 12);
      if (this->rx_left_ > MAX_PAYLOAD) {
        ESP_LOGW(TAG, "Frame too large: %u", (unsigned) this->rx_left_);
        this->disconnect_();
        return;
      }
      this->rx_small_.clear();
      if (this->rx_type_ == FRAME_AUDIO && this->rx_rate_ != 0) {
        this->tts_rate_.store(this->rx_rate_);
        this->tts_channels_.store(this->rx_channels_ ? this->rx_channels_ : 1);
      }
      if (this->rx_left_ == 0) {
        this->handle_frame_();
        this->rx_hdr_got_ = 0;
      }
      continue;
    }

    size_t want = std::min<size_t>(this->rx_left_, sizeof(tmp));
    if (this->rx_type_ == FRAME_AUDIO) {
      size_t room = this->rx_ring_->free();
      if (room == 0)
        return;  // TCP backpressure paces the server to playback speed
      want = std::min(want, room);
    }
    ssize_t r = ::recv(this->fd_, tmp, want, MSG_DONTWAIT);
    if (r == 0) {
      this->disconnect_();
      return;
    }
    if (r < 0) {
      if (errno != EAGAIN && errno != EWOULDBLOCK)
        this->disconnect_();
      return;
    }
    this->last_rx_ms_ = millis();
    if (this->rx_type_ == FRAME_AUDIO) {
      this->rx_ring_->write(tmp, (size_t) r);
    } else if (this->rx_small_.size() < 1024) {
      this->rx_small_.insert(this->rx_small_.end(), tmp, tmp + r);
    }
    this->rx_left_ -= (uint32_t) r;
    if (this->rx_left_ == 0) {
      this->handle_frame_();
      this->rx_hdr_got_ = 0;
    }
  }
}

void HaVoiceSat::net_run_() {
  std::vector<uint8_t> buf(TX_CHUNK);
  std::vector<std::string> events;
  while (true) {
    if (this->host_changed_.exchange(false))
      this->disconnect_();

    if (this->fd_ < 0) {
      uint32_t now = millis();
      if (!network::is_connected() ||
          (this->last_connect_try_ms_ != 0 && now - this->last_connect_try_ms_ < RECONNECT_MS)) {
        vTaskDelay(pdMS_TO_TICKS(200));
        continue;
      }
      this->last_connect_try_ms_ = now;
      if (!this->connect_())
        continue;
      this->send_hello_();
      continue;
    }

    {
      std::lock_guard<std::mutex> lock(this->events_mutex_);
      events.swap(this->events_);
    }
    for (auto &e : events)
      this->send_frame_(FRAME_EVENT, reinterpret_cast<const uint8_t *>(e.data()), e.size());
    events.clear();

    size_t n = this->tx_ring_->read(buf.data(), buf.size(), pdMS_TO_TICKS(20));
    if (n > 0) {
      auto info = this->mic_->get_audio_stream_info();
      size_t frame = info.get_bits_per_sample() / 8 * info.get_channels();
      if (frame == 0)
        frame = 2;
      size_t rem = n % frame;
      if (rem != 0 && n + frame - rem <= buf.size()) {
        n += this->tx_ring_->read(buf.data() + n, frame - rem, pdMS_TO_TICKS(20));
      }
      n -= n % frame;
      if (n > 0)
        this->send_frame_(FRAME_AUDIO, buf.data(), n, info.get_sample_rate(), info.get_channels(),
                          info.get_bits_per_sample());
    } else if (millis() - this->last_tx_ms_ > PING_MS) {
      this->send_frame_(FRAME_PING, nullptr, 0);
    }

    this->poll_recv_();

    if (this->fd_ >= 0 && millis() - this->last_rx_ms_ > RX_TIMEOUT_MS) {
      ESP_LOGW(TAG, "Server silent for %us, reconnecting", (unsigned) (RX_TIMEOUT_MS / 1000));
      this->disconnect_();
    }
  }
}

}  // namespace ha_voice_sat
}  // namespace esphome
