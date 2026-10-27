function reply = ultramic_command(command, label)
% Enable software trigger and arm triggers in Ultramic Studio first.
% Examples: ultramic_command("START", "mouse_01_trial_02")
%           ultramic_command("STOP")
% Requires a MATLAB version providing tcpclient, writeline, and readline.
    client = tcpclient("127.0.0.1", 8765, "Timeout", 6);
    configureTerminator(client, "LF");
    request = struct("command", char(command));
    if nargin > 1
        request.label = char(label);
    end
    writeline(client, jsonencode(request));
    reply = jsondecode(readline(client));
    clear client;
    if ~reply.ok
        error("Ultramic:TriggerError", "%s", reply.error);
    end
end
