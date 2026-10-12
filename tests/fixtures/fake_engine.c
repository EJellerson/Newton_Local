/*
 * Newton_Local test fixture — a FAKE model engine for
 * scripts/prove-start-supervision.py. It contains no model code and cannot
 * load weights; it only imitates how a real engine behaves at start-up, so
 * the controller can be proven against REAL launchd jobs without ever
 * putting a model in memory.
 *
 *   fake-engine --version         print a version line, exit 0
 *                                 (the registry's availability probe)
 *   fake-engine --mode oom ...    print mlx-serve v26.9.1's real refusal
 *                                 lines (2026-10-12 incident) and exit 1
 *   fake-engine --mode hang ...   print a loading line, never bind, wait
 *                                 forever (until signalled)
 *   fake-engine --mode ok ...     wait --load-seconds, then answer
 *                                 GET /health and GET /props on --host:--port
 *
 * It is a compiled binary on purpose: the controller recognises an engine
 * process by its argv[0] being a registered runtime executable, which a
 * shebang script would not satisfy.
 */
#include <arpa/inet.h>
#include <netinet/in.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/socket.h>
#include <unistd.h>

static const char *arg(int argc, char **argv, const char *name, const char *def) {
    for (int i = 1; i < argc - 1; i++)
        if (strcmp(argv[i], name) == 0) return argv[i + 1];
    return def;
}

int main(int argc, char **argv) {
    for (int i = 1; i < argc; i++)
        if (strcmp(argv[i], "--version") == 0) {
            printf("fake-engine 1.0 (Newton_Local test fixture; contains no model code)\n");
            return 0;
        }
    const char *mode = arg(argc, argv, "--mode", "ok");
    const char *host = arg(argc, argv, "--host", "127.0.0.1");
    const char *alias = arg(argc, argv, "--alias", "fixture");
    int port = atoi(arg(argc, argv, "--port", "0"));
    double load = atof(arg(argc, argv, "--load-seconds", "1"));
    setvbuf(stderr, NULL, _IONBF, 0);

    if (strcmp(mode, "oom") == 0) {
        fprintf(stderr, "[preflight] weights ~70.13 GB, available 68.98 GB\n");
        fprintf(stderr, "Insufficient memory to load model: needs ~77.1 GB free (70.1 GB of "
                        "weights plus headroom for warmup buffers and a baseline KV cache) but "
                        "only 69.0 GB is available. Close other models/apps (or wait for a "
                        "prior mlx-serve to fully exit) and retry; pass --skip-mem-preflight "
                        "to override.\n");
        fprintf(stderr, "[scheduler] model load failed: InsufficientMemory\n");
        fprintf(stderr, "error: LoadFailed\n");
        return 1;
    }
    if (strcmp(mode, "hang") == 0) {
        fprintf(stderr, "[fake-engine] loading (this fixture never finishes)\n");
        for (;;) pause();
    }

    fprintf(stderr, "[fake-engine] loading for %.1fs\n", load);
    usleep((useconds_t)(load * 1e6));
    int s = socket(AF_INET, SOCK_STREAM, 0);
    int one = 1;
    setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &one, sizeof one);
    struct sockaddr_in a;
    memset(&a, 0, sizeof a);
    a.sin_family = AF_INET;
    a.sin_port = htons((unsigned short)port);
    if (inet_pton(AF_INET, host, &a.sin_addr) != 1) {
        fprintf(stderr, "error: bad --host %s\n", host);
        return 2;
    }
    if (bind(s, (struct sockaddr *)&a, sizeof a) != 0) {
        fprintf(stderr, "error: AddressInUse (port %d)\n", port);
        return 3;
    }
    listen(s, 16);
    fprintf(stderr, "[fake-engine] serving on %s:%d\n", host, port);

    char req[4096], body[512], resp[1024];
    for (;;) {
        int c = accept(s, NULL, NULL);
        if (c < 0) continue;
        ssize_t n = read(c, req, sizeof req - 1);
        if (n <= 0) { close(c); continue; }
        req[n] = 0;
        int code = 200;
        if (strncmp(req, "GET /health", 11) == 0)
            snprintf(body, sizeof body, "{\"status\":\"ok\"}");
        else if (strncmp(req, "GET /props", 10) == 0)
            snprintf(body, sizeof body,
                     "{\"model_alias\":\"%s\",\"model_path\":\"fixture\","
                     "\"default_generation_settings\":{\"n_ctx\":4096},\"total_slots\":1}",
                     alias);
        else {
            code = 404;
            snprintf(body, sizeof body, "{\"error\":\"not found\"}");
        }
        int m = snprintf(resp, sizeof resp,
                         "HTTP/1.1 %d %s\r\nContent-Type: application/json\r\n"
                         "Content-Length: %zu\r\nConnection: close\r\n\r\n%s",
                         code, code == 200 ? "OK" : "Not Found", strlen(body), body);
        if (write(c, resp, (size_t)m) < 0) { /* client went away */ }
        close(c);
    }
}
