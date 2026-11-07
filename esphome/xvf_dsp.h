// XVF3800 audio parameters set from the HA Voice panel, written over I2C and read
// back, so the panel shows what the chip really holds. The chip resets them on
// power-up; HA Voice sends them again on every connection.
//
// (resid, cmd) come from Respeaker xvf_host.py, written for the USB firmware. The
// read-back is what tells whether they hold on the I2S firmware too.
#pragma once

#include <cmath>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>

#include "esphome/components/i2c/i2c.h"

namespace xvf_dsp {

using Device = esphome::i2c::I2CDevice;

struct Param {
  const char *key;
  uint8_t resid;
  uint8_t cmd;
  bool is_float;  // else int32
  float want;
  uint8_t state;
  uint8_t writes;
  uint8_t reads;
};

enum : uint8_t { IDLE, WRITE, READ };

static const uint8_t PP = 17;
static const uint8_t AEC = 33;
static const uint8_t AZIMUTH_CMD = 75;
static const uint8_t FIXED_ON_CMD = 37;
static const uint8_t FIXED_AZIMUTH_CMD = 81;
static const uint8_t READ_BIT = 0x80;
static const uint8_t STATUS_DONE = 0;
static const uint8_t STATUS_WAIT = 1;
static const uint8_t STATUS_RETRY = 0x40;  // the servicer has no fresh data yet
static const uint8_t MAX_WRITES = 3;
static const uint8_t MAX_READS = 20;

static Param PARAMS[] = {
    {"min_ns", PP, 21, true, 0, IDLE, 0, 0},
    {"min_nn", PP, 22, true, 0, IDLE, 0, 0},
    {"attns_mode", PP, 32, false, 0, IDLE, 0, 0},
    {"attns_nominal", PP, 33, true, 0, IDLE, 0, 0},
    {"attns_slope", PP, 34, true, 0, IDLE, 0, 0},
    {"hpf", AEC, 1, false, 0, IDLE, 0, 0},
    {"agc_on", PP, 10, false, 0, IDLE, 0, 0},
    {"agc_max_gain", PP, 11, true, 0, IDLE, 0, 0},
    {"agc_level", PP, 12, true, 0, IDLE, 0, 0},
    {"agc_time", PP, 14, true, 0, IDLE, 0, 0},
    {"asr_gain", AEC, 36, true, 0, IDLE, 0, 0},
};

// Queues a write; false if the key is not one of the parameters.
inline bool set(const std::string &key, float value) {
  for (auto &p : PARAMS) {
    if (key == p.key) {
      p.want = value;
      p.state = WRITE;
      p.writes = 0;
      p.reads = 0;
      return true;
    }
  }
  return false;
}

// Reads what the chip holds, writes nothing.
inline void read_all() {
  for (auto &p : PARAMS) {
    if (p.state == IDLE) {
      p.state = READ;
      p.writes = MAX_WRITES;
      p.reads = 0;
    }
  }
}

inline bool send(Device &dev, uint8_t resid, uint8_t cmd, const void *value, uint8_t len) {
  uint8_t payload[3 + 8];
  payload[0] = resid;
  payload[1] = cmd;
  payload[2] = len;
  memcpy(&payload[3], value, len);
  return dev.write(payload, 3 + len) == esphome::i2c::ERROR_OK;
}

// Handles one parameter per call: the bus is shared with the LED ring, and a read
// usually needs a second try once the chip has the value ready. Returns the report
// for HA Voice, "key=value" or "key=?" when the chip gives no value, else "".
inline std::string tick(Device &dev) {
  for (auto &p : PARAMS) {
    if (p.state == IDLE)
      continue;
    if (p.state == WRITE) {
      if (p.is_float) {
        send(dev, p.resid, p.cmd, &p.want, 4);
      } else {
        const int32_t v = (int32_t) p.want;
        send(dev, p.resid, p.cmd, &v, 4);
      }
      p.writes++;
      p.reads = 0;
      p.state = READ;
      return "";
    }
    const uint8_t req[] = {p.resid, (uint8_t) (p.cmd | READ_BIT), 5};
    uint8_t resp[5] = {0};
    const bool ok = dev.write_read(req, sizeof(req), resp, sizeof(resp)) == esphome::i2c::ERROR_OK;
    if (ok && (resp[0] == STATUS_WAIT || resp[0] == STATUS_RETRY) && ++p.reads < MAX_READS)
      return "";
    if (!ok || resp[0] != STATUS_DONE) {
      p.state = IDLE;
      return std::string(p.key) + "=?";
    }
    float got;
    if (p.is_float) {
      memcpy(&got, &resp[1], 4);
    } else {
      int32_t v;
      memcpy(&v, &resp[1], 4);
      got = (float) v;
    }
    if (p.writes < MAX_WRITES && fabsf(got - p.want) > fabsf(p.want) * 0.01f + 1e-7f) {
      p.state = WRITE;
      return "";
    }
    p.state = IDLE;
    char buf[48];
    snprintf(buf, sizeof(buf), "%s=%g", p.key, got);
    return buf;
  }
  return "";
}

// True while parameters wait for the bus: the direction poll keeps off then.
inline bool busy() {
  for (auto &p : PARAMS) {
    if (p.state != IDLE)
      return true;
  }
  return false;
}

// Both fixed beams on one direction, degrees.
inline void fix_beam(Device &dev, float degrees) {
  const float rad = fmodf(degrees, 360.0f) * (float) M_PI / 180.0f;
  const float both[2] = {rad, rad};
  send(dev, AEC, FIXED_AZIMUTH_CMD, both, sizeof(both));
  const int32_t on = 1;
  send(dev, AEC, FIXED_ON_CMD, &on, sizeof(on));
}

// Direction of the last voice, degrees 0..359. No fresh value in silence: false.
// `free_running`: with fixed beams only the free-running beam still follows the voice.
inline bool read_direction(Device &dev, bool free_running, int &degrees) {
  const uint8_t req[] = {AEC, (uint8_t) (AZIMUTH_CMD | READ_BIT), 17};
  uint8_t resp[17];
  if (dev.write_read(req, sizeof(req), resp, sizeof(resp)) != esphome::i2c::ERROR_OK || resp[0] != STATUS_DONE)
    return false;
  float rad;
  memcpy(&rad, &resp[1 + (free_running ? 2 : 3) * 4], 4);
  if (std::isnan(rad))
    return false;
  degrees = ((int) lroundf(rad * 180.0f / (float) M_PI) % 360 + 360) % 360;
  return true;
}

}  // namespace xvf_dsp
