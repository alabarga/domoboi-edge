import time, sys, re, select, termios, os

port = sys.argv[1]
start_time = time.time()
registered = False
rssi = 99
attempt = 0

def send_cmd(cmd, wait=1.0):
    fd = os.open(port, os.O_RDWR | os.O_NOCTTY)
    attrs = termios.tcgetattr(fd)
    attrs[2] |= termios.CLOCAL | termios.CRTSCTS
    termios.tcsetattr(fd, termios.TCSANOW, attrs)
    termios.tcflush(fd, termios.TCIOFLUSH)
    os.write(fd, cmd + b'\r\n')
    time.sleep(wait)
    r, _, _ = select.select([fd], [], [], 0.5)
    resp = b""
    if r:
        resp = os.read(fd, 1024)
    os.close(fd)
    return resp.decode(errors='ignore')

while time.time() - start_time < 150:
    attempt += 1
    cpin_resp = send_cmd(b'AT+CPIN?')
    cpin_state = 'UNKNOWN'
    if 'READY' in cpin_resp:
        cpin_state = 'READY'
    elif 'SIM PIN' in cpin_resp:
        cpin_state = 'PIN_LOCKED'
    elif 'NOT INSERTED' in cpin_resp:
        cpin_state = 'NO_SIM'
    creg_resp = send_cmd(b'AT+CREG?')
    creg_state = 'UNKNOWN'
    reg_match = re.search(r'\\+CREG:\s*(?:\d\s*,\s*)?([0-9])', creg_resp)
    if reg_match:
        status = int(reg_match.group(1))
        if status == 0:
            creg_state = 'NOT_REG_NOT_SEARCHING'
        elif status == 1:
            creg_state = 'REGISTERED_HOME'
        elif status == 2:
            creg_state = 'SEARCHING'
        elif status == 3:
            creg_state = 'REGISTRATION_DENIED'
        elif status == 4:
            creg_state = 'UNKNOWN'
        elif status == 5:
            creg_state = 'REGISTERED_ROAMING'
        if status in (1, 5):
            registered = True
    csq_resp = send_cmd(b'AT+CSQ')
    csq_state = 'UNKNOWN'
    for line in csq_resp.split('\n'):
        if '+CSQ:' in line:
            csq_state = line.split(':')[1].strip()
            try:
                rssi = int(line.split(':')[1].split(',')[0].strip())
            except Exception:
                pass
    elapsed = int(time.time() - start_time)
    print(f'  [Attempt {attempt}] SIM: {cpin_state} | Net: {creg_state} | Signal: {csq_state} | Elapsed: {elapsed}s')
    if registered:
        break
    time.sleep(8)

if registered:
    print(f'SUCCESS: Registered on network. Signal strength RSSI: {rssi}/31')
    sys.exit(0)
else:
    print('TIMEOUT: Modem failed to register within 150 seconds.')
    sys.exit(1)
