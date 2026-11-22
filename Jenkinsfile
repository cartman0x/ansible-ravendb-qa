// Weekly QA run: harness unit tests + embedded-RavenDB smoke tests, optionally against a
// RavenDB nightly build (parameter NIGHTLY_SERVER_URL).
//
// Run it from the `main` branch (that is where tests/ lives).
//
// Needs on the agent:
//   - Linux, python3 (>= 3.10) with the venv module
//   - .NET runtime that the RavenDB server requires
//   - outbound network (pip install; embedded server package / nightly download)
//   - Jenkins "Secret file" credential with a RavenDB Developer license.json
//     (id: ravendb-qa-license). Without it, multi-node smoke tests are SKIPPED, not failed.
//   - Slack Notification plugin configured, with its bot invited to #functional-qa-weekly
//
// It does NOT use Docker, /etc/hosts, sudo, fixed ports or the lab containers, so it
// cannot collide with a scenario run on the same machine (it only competes for CPU/RAM).

pipeline {
    agent { label 'qa-linux' }          // ask the admin for the real agent label

    triggers {
        cron('H 3 * * 0')               // weekly, Sunday ~03:00 (H = Jenkins picks the minute)
    }

    parameters {
        string(name: 'NIGHTLY_SERVER_URL', defaultValue: '',
               description: 'URL of a RavenDB server archive (.zip / .tar.*) to test. ' +
                            'Empty = the server bundled with ravendb-embedded.')
    }

    options {
        timestamps()
        disableConcurrentBuilds()
        timeout(time: 60, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '30'))
    }

    environment {
        PYTHONDONTWRITEBYTECODE = '1'
        PYTHONPATH              = '.'
        VENV                    = "${WORKSPACE}/.venv"
        SERVER_DIR              = "${WORKSPACE}/raven-server"
        SLACK_CHANNEL           = '#functional-qa-weekly'
        QA_NOTIFY               = ''   // a team address for failure mails; empty = no mail
    }

    stages {
        stage('Setup') {
            steps {
                sh '''
                    set -eu
                    python3 -m venv "$VENV"
                    "$VENV/bin/pip" install --quiet --upgrade pip
                    "$VENV/bin/pip" install --quiet -r tests/requirements.txt
                    command -v dotnet >/dev/null || { echo "ERROR: dotnet runtime not found on agent"; exit 1; }
                    mkdir -p reports
                '''
            }
        }

        stage('Fetch RavenDB build') {
            when { expression { params.NIGHTLY_SERVER_URL?.trim() } }
            steps {
                // The folder holding Raven.Server.dll goes to $SERVER_DIR/.location for the smoke stage.
                sh '''
                    set -eu
                    rm -rf "$SERVER_DIR"; mkdir -p "$SERVER_DIR"
                    echo "Testing RavenDB build from: $NIGHTLY_SERVER_URL"
                    curl -fsSL -o "$SERVER_DIR/server.pkg" "$NIGHTLY_SERVER_URL"
                    case "$NIGHTLY_SERVER_URL" in
                        *.zip)         unzip -q "$SERVER_DIR/server.pkg" -d "$SERVER_DIR/unpacked" ;;
                        *.tar.*|*.tgz) mkdir "$SERVER_DIR/unpacked" && tar -xf "$SERVER_DIR/server.pkg" -C "$SERVER_DIR/unpacked" ;;
                        *)             echo "ERROR: unknown archive type"; exit 1 ;;
                    esac
                    DLL="$(find "$SERVER_DIR/unpacked" -name Raven.Server.dll | head -n1)"
                    [ -n "$DLL" ] || { echo "ERROR: Raven.Server.dll not found in the archive"; exit 1; }
                    dirname "$DLL" > "$SERVER_DIR/.location"
                    echo "Server directory: $(cat "$SERVER_DIR/.location")"
                '''
            }
        }

        stage('Unit tests') {
            steps {
                // If this fails the harness itself is broken, so the smoke stage does not run.
                sh '"$VENV/bin/pytest" tests/unit -v -p no:cacheprovider --junitxml=reports/unit.xml'
            }
            post {
                failure { junit 'reports/unit.xml' }
            }
        }

        stage('Smoke (embedded RavenDB)') {
            steps {
                withCredentials([file(credentialsId: 'ravendb-qa-license', variable: 'RAVEN_TEST_LICENSE')]) {
                    sh '''
                        if [ -f "$SERVER_DIR/.location" ]; then
                            export RAVEN_TEST_SERVER_DIR="$(cat "$SERVER_DIR/.location")"
                            echo "Using RavenDB server at $RAVEN_TEST_SERVER_DIR"
                        fi
                        "$VENV/bin/pytest" tests/integration -v -p no:cacheprovider --junitxml=reports/smoke.xml
                    '''
                }
            }
            post {
                always { junit 'reports/smoke.xml' }
            }
        }
    }

    post {
        always {
            script {
                def color = currentBuild.currentResult == 'SUCCESS' ? 'good' : 'danger'
                def build = params.NIGHTLY_SERVER_URL?.trim() ? "nightly ${params.NIGHTLY_SERVER_URL}" : 'bundled embedded server'
                slackSend channel: env.SLACK_CHANNEL, color: color,
                          message: "QA weekly (functional): *${currentBuild.currentResult}* - " +
                                   "${env.JOB_NAME} #${env.BUILD_NUMBER}\nRavenDB: ${build}\n${env.BUILD_URL}"
            }
        }
        unsuccessful {
            script {
                if (env.QA_NOTIFY?.trim()) {
                    mail to: env.QA_NOTIFY,
                         subject: "QA weekly run ${currentBuild.currentResult}: ${env.JOB_NAME} #${env.BUILD_NUMBER}",
                         body: "See ${env.BUILD_URL}\nCommit: ${env.GIT_COMMIT ?: 'n/a'}"
                }
            }
        }
        cleanup {
            sh 'rm -rf "$VENV" "$SERVER_DIR"'
        }
    }
}
