pipeline {
    agent any

    options {
        timeout(time: 30, unit: 'MINUTES')
        buildDiscarder(logRotator(numToKeepStr: '10'))
    }

    environment {
        PYTHONUNBUFFERED = '1'
        PIP_DISABLE_PIP_VERSION_CHECK = '1'
    }

    stages {
        stage('Checkout') {
            steps {
                checkout scm
                sh '''
                    git log -1 --oneline
                    git status --short
                '''
            }
        }

        stage('Python 环境') {
            steps {
                sh '''
                    set -eux
                    python3 --version
                    python3 -m venv .venv
                    . .venv/bin/activate
                    python -m pip install --upgrade pip
                    python -m pip install -e .
                    python -c "import beancount, yaml; print('beancount', beancount.__version__); print('pyyaml', yaml.__version__)"
                '''
            }
        }

        stage('测试') {
            steps {
                sh '''
                    set -eux
                    . .venv/bin/activate
                    python -m unittest discover -s tests -t . -v
                '''
            }
        }

        stage('CLI 冒烟') {
            steps {
                sh '''
                    set -eux
                    . .venv/bin/activate
                    ledger-kernel --version
                    ledger-kernel --help > /dev/null
                '''
            }
        }
    }

    post {
        success {
            echo '✅ LedgerKernel CI 通过'
        }
        failure {
            echo '❌ LedgerKernel CI 失败，请查看测试日志'
        }
        always {
            cleanWs()
        }
    }
}
