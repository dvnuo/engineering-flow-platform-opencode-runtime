from fnmatch import fnmatchcase

from efp_opencode_adapter.permission_generator import profile_policy_permission_baseline


def _policy_for(command: str) -> str | None:
    bash = profile_policy_permission_baseline()["bash"]
    for pattern, policy in bash.items():
        if fnmatchcase(command, pattern):
            return policy
    return None


def test_profile_policy_allows_aws_auth_and_read_only_aws_calls():
    assert _policy_for("aws-auth account list --json") == "allow"
    assert _policy_for("aws-auth login --account cps-dev --json") == "allow"
    assert _policy_for("aws-auth eks kubeconfig --account cps-dev --cluster c1 --json") == "allow"
    assert _policy_for("aws --profile cps-dev sts get-caller-identity --output json") == "ask"
    assert _policy_for("aws sts get-caller-identity --profile cps-dev --output json") == "allow"
    assert _policy_for("aws eks list-clusters --profile cps-dev --region ap-east-1 --output json") == "allow"
    assert _policy_for("aws ecr describe-images --repository-name app --output json") == "allow"
    assert _policy_for("aws logs filter-log-events --log-group-name /app --output json") == "allow"
    assert _policy_for("aws ec2 terminate-instances --instance-ids i-1") == "ask"
    assert _policy_for("aws ecr batch-delete-image --repository-name app") == "ask"
    assert _policy_for("aws s3 rm s3://bucket/key") == "ask"


def test_profile_policy_keeps_kubectl_read_only_and_asks_for_secrets():
    assert _policy_for("kubectl --context cps-dev/c1 get pods -n payments") == "ask"
    assert _policy_for("kubectl get pods -n payments --context cps-dev/c1") == "allow"
    assert _policy_for("kubectl describe pod api-1 -n payments") == "allow"
    assert _policy_for("kubectl logs api-1 -n payments --tail 200") == "allow"
    assert _policy_for("kubectl top pods -n payments") == "allow"
    assert _policy_for("kubectl auth can-i list pods --all-namespaces") == "allow"
    assert _policy_for("kubectl config get-contexts") == "allow"
    assert _policy_for("kubectl get secret db-creds -n payments -o yaml") == "ask"
    assert _policy_for("kubectl get secrets -n payments") == "ask"
    assert _policy_for("kubectl describe secret db-creds -n payments") == "ask"
    for mutating in (
        "kubectl apply -f deploy.yaml",
        "kubectl delete pod api-1 -n payments",
        "kubectl scale deploy api --replicas 0",
        "kubectl rollout restart deploy api",
        "kubectl exec -it api-1 -- sh",
        "kubectl port-forward svc/api 8080:80",
        "kubectl edit deploy api",
        "kubectl patch deploy api -p {}",
    ):
        assert _policy_for(mutating) == "ask", mutating
