/* Ultramic Studio TTL -> USB serial bridge, Arduino Uno R3 / classic Nano.
   GATE_MODE=true: D2 high starts, D2 low stops.
   GATE_MODE=false: rising edge D2 starts, rising edge D3 stops.
   Inputs need external 10k pulldowns to ground. Common ground with source.
   Only 0–5 V logic on this 5 V board; never connect RS-232 or bipolar pulses.
   USB transport is asynchronous to the microphone sample clock.
*/
const bool GATE_MODE = true;
const uint8_t N = 32; // At most 31 queued edges; overflow is reported, not hidden.
volatile uint8_t head = 0, tail = 0;
volatile uint32_t stamp[N];
volatile bool starts[N];
volatile bool overflowed = false;
bool failed = false;
uint32_t heartbeatAt = 0;
char input[16];
uint8_t inputLength = 0;

void enqueue(bool start) {
  uint8_t next = (head + 1) % N;
  if (next == tail) { overflowed = true; return; }
  stamp[head] = micros();
  starts[head] = start;
  head = next;
}
void gateEdge() { enqueue(digitalRead(2) == HIGH); }
void startEdge() { enqueue(true); }
void stopEdge() { enqueue(false); }

void sendEvent(bool start, uint32_t when) {
  Serial.print(start ? F("{\"command\":\"START\",\"device_us\":")
                     : F("{\"command\":\"STOP\",\"device_us\":"));
  Serial.print(when);
  Serial.println(F("}"));
}

void setup() {
  pinMode(2, INPUT);
  pinMode(3, INPUT);
  Serial.begin(115200);
  if (GATE_MODE) attachInterrupt(digitalPinToInterrupt(2), gateEdge, CHANGE);
  else {
    attachInterrupt(digitalPinToInterrupt(2), startEdge, RISING);
    attachInterrupt(digitalPinToInterrupt(3), stopEdge, RISING);
  }
  Serial.print(F("{\"command\":\"READY\",\"mode\":\""));
  Serial.print(GATE_MODE ? F("gate") : F("separate start/stop"));
  Serial.println(F("\"}"));
}

void loop() {
  if (overflowed && !failed) {
    failed = true;
    detachInterrupt(digitalPinToInterrupt(2));
    detachInterrupt(digitalPinToInterrupt(3));
    Serial.println(F("{\"command\":\"ERROR\",\"message\":\"TTL edge queue overflow. Reset bridge.\"}"));
  }
  if (!failed) {
    while (tail != head) {
      noInterrupts();
      uint32_t when = stamp[tail];
      bool start = starts[tail];
      tail = (tail + 1) % N;
      interrupts();
      sendEvent(start, when);
    }
  }
  while (Serial.available()) {
    char ch = Serial.read();
    if (ch == '\n') {
      input[inputLength] = 0;
      if (!failed && GATE_MODE && strcmp(input, "SYNC") == 0) {
        noInterrupts();
        // Queue the current level behind any edges already captured.
        enqueue(digitalRead(2) == HIGH);
        interrupts();
      }
      inputLength = 0;
    } else if (ch != '\r') {
      if (inputLength < sizeof(input) - 1) input[inputLength++] = ch;
      else inputLength = 0;
    }
  }
  if (millis() - heartbeatAt >= 1000) {
    heartbeatAt = millis();
    if (failed) Serial.println(F("{\"command\":\"ERROR\",\"message\":\"Bridge halted; reset required.\"}"));
    else Serial.println(F("{\"command\":\"HEARTBEAT\"}"));
  }
}
