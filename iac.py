#!/usr/bin/env python3
"""
Boto3 IAC automation to deploy Auto Scaling Group with Network Load Balancer

Usage:
    # Option 1: Set environment variables
    export VPC_ID=vpc-xxxxx
    export SUBNET_IDS=subnet-xxxxx,subnet-yyyyy
    python iac.py
    
    # Option 2: Pass as command line arguments
    python iac.py vpc-xxxxx subnet-xxxxx,subnet-yyyyy
    
    # Option 3: Interactive prompt (will ask if not provided)
    python iac.py

To find VPC and Subnet IDs:
    - Check AWS Console: VPC Dashboard
    - From EC2 instance: curl http://169.254.169.254/latest/meta-data/network/interfaces/macs/<mac>/vpc-id
    - From launch template: Check the template configuration in AWS Console
"""

import boto3
import os
import re
import time
import ipaddress
from typing import Optional, List, Dict, Tuple
from botocore.exceptions import ClientError

# AWS Configuration
AMI_ID = "ami-07b9762960a9da859"
LAUNCH_TEMPLATE_ID = "lt-0eb3866711e320093"
REGION = os.getenv("AWS_DEFAULT_REGION", "us-east-1")

# Network Configuration - can be set via environment variables
VPC_ID = os.getenv("VPC_ID", "")
SUBNET_IDS = os.getenv("SUBNET_IDS", "")  # Comma-separated list

# Resource naming
NLB_NAME = "cpu-stresser-nlb"
TARGET_GROUP_NAME = "cpu-stresser-tg"
ASG_NAME = "cpu-stresser-asg"


class AWSInfrastructure:
    def __init__(self, region: str = REGION):
        """Initialize AWS clients"""
        self.region = region
        self.ec2 = boto3.client("ec2", region_name=region)
        self.elbv2 = boto3.client("elbv2", region_name=region)
        self.autoscaling = boto3.client("autoscaling", region_name=region)
        self.final_subnets = []  # Store final subnet list after NLB creation
        
    def get_vpc_info(self, vpc_id: str) -> Optional[Dict]:
        """Get VPC information including CIDR blocks"""
        try:
            response = self.ec2.describe_vpcs(VpcIds=[vpc_id])
            if not response["Vpcs"]:
                return None
            vpc = response["Vpcs"][0]
            return {
                "VpcId": vpc["VpcId"],
                "CidrBlock": vpc["CidrBlock"],
                "CidrBlockAssociationSet": vpc.get("CidrBlockAssociationSet", [])
            }
        except ClientError as e:
            print(f"Error getting VPC info: {e}")
            return None
    
    def check_existing_subnet_space(self, vpc_id: str, exclude_subnet_ids: List[str]) -> Optional[str]:
        """Check if any existing subnet in a different AZ has enough IP space (at least 8 free IPs)"""
        existing_subnets = self.get_existing_subnets(vpc_id)
        exclude_set = set(exclude_subnet_ids)
        
        for subnet in existing_subnets:
            if subnet["SubnetId"] in exclude_set:
                continue
            
            available_ips = subnet.get("AvailableIpAddressCount", 0)
            if available_ips >= 8:
                print(f"  ✓ Found existing subnet with {available_ips} available IPs: {subnet['SubnetId']} ({subnet['CidrBlock']}) in {subnet['AvailabilityZone']}")
                return subnet["SubnetId"]
        
        return None
    
    def get_existing_subnets(self, vpc_id: str) -> List[Dict]:
        """Get all existing subnets in the VPC"""
        try:
            response = self.ec2.describe_subnets(
                Filters=[{"Name": "vpc-id", "Values": [vpc_id]}]
            )
            return response.get("Subnets", [])
        except ClientError as e:
            print(f"Error getting subnets: {e}")
            return []
    
    def find_available_cidr(self, vpc_id: str, subnet_size: int = 24) -> Optional[Tuple[str, str]]:
        """Find an available CIDR block for a new subnet"""
        vpc_info = self.get_vpc_info(vpc_id)
        if not vpc_info:
            print("  ✗ Could not get VPC information")
            return None
        
        # Get all VPC CIDR blocks (primary + secondary)
        vpc_cidrs = [vpc_info["CidrBlock"]]
        for assoc in vpc_info.get("CidrBlockAssociationSet", []):
            if assoc.get("CidrBlockState", {}).get("State") == "associated":
                cidr = assoc.get("CidrBlock")
                if cidr and cidr not in vpc_cidrs:
                    vpc_cidrs.append(cidr)
        
        print(f"  VPC CIDR blocks: {vpc_cidrs}")
        
        # Get existing subnets
        existing_subnets = self.get_existing_subnets(vpc_id)
        print(f"  Found {len(existing_subnets)} existing subnets")
        used_networks = []
        for subnet in existing_subnets:
            cidr = subnet["CidrBlock"]
            used_networks.append(ipaddress.ip_network(cidr))
            print(f"    - {subnet['SubnetId']}: {cidr} ({subnet['AvailabilityZone']})")
        
        # Try multiple subnet sizes, starting with the requested size
        subnet_sizes = [subnet_size, 25, 26, 27] if subnet_size >= 24 else [subnet_size]
        
        # Try each VPC CIDR block
        for vpc_cidr in vpc_cidrs:
            print(f"  Checking CIDR block: {vpc_cidr}")
            vpc_network = ipaddress.ip_network(vpc_cidr)
            
            for size in subnet_sizes:
                print(f"  Trying to find available /{size} subnet in {vpc_cidr}...")
                try:
                    # Try forward search first
                    for subnet in vpc_network.subnets(new_prefix=size):
                        # Check if this subnet overlaps with any existing subnet
                        overlaps = any(subnet.overlaps(used) for used in used_networks)
                        if not overlaps:
                            # Find an available AZ (prefer different AZ, but can use same if needed)
                            existing_azs = {s["AvailabilityZone"] for s in existing_subnets}
                            all_azs = self.get_available_zones()
                            if not all_azs:
                                print("  ✗ No available AZs found")
                                continue
                            
                            # Prefer an AZ that doesn't have subnets yet, but fall back to any AZ
                            available_az = next((az for az in all_azs if az not in existing_azs), all_azs[0])
                            print(f"  ✓ Found available CIDR: {subnet} in {available_az}")
                            return str(subnet), available_az
                    
                    # If forward search didn't work, try reverse (from end of VPC range)
                    print(f"  Forward search failed, trying reverse search...")
                    all_subnets = list(vpc_network.subnets(new_prefix=size))
                    for subnet in reversed(all_subnets):
                        overlaps = any(subnet.overlaps(used) for used in used_networks)
                        if not overlaps:
                            existing_azs = {s["AvailabilityZone"] for s in existing_subnets}
                            all_azs = self.get_available_zones()
                            if not all_azs:
                                continue
                            available_az = next((az for az in all_azs if az not in existing_azs), all_azs[0])
                            print(f"  ✓ Found available CIDR: {subnet} in {available_az}")
                            return str(subnet), available_az
                            
                except ValueError as e:
                    # VPC CIDR too small for this subnet size
                    print(f"  VPC CIDR too small for /{size} subnets: {e}")
                    continue
        
        print("  ✗ No available CIDR blocks found in any VPC CIDR block")
        print("  Tip: You may need to add a secondary CIDR block to your VPC, or manually create a subnet")
        return None
    
    def suggest_subnet_cidr(self, vpc_id: str) -> Optional[Tuple[str, str]]:
        """Suggest a subnet CIDR block based on VPC structure (checks for conflicts)"""
        vpc_info = self.get_vpc_info(vpc_id)
        if not vpc_info:
            return None
        
        # Get all VPC CIDR blocks
        vpc_cidrs = [vpc_info["CidrBlock"]]
        for assoc in vpc_info.get("CidrBlockAssociationSet", []):
            if assoc.get("CidrBlockState", {}).get("State") == "associated":
                cidr = assoc.get("CidrBlock")
                if cidr and cidr not in vpc_cidrs:
                    vpc_cidrs.append(cidr)
        
        # Get existing subnets to check for conflicts and find an available AZ
        existing_subnets = self.get_existing_subnets(vpc_id)
        used_networks = [ipaddress.ip_network(s["CidrBlock"]) for s in existing_subnets]
        existing_azs = {s["AvailabilityZone"] for s in existing_subnets}
        all_azs = self.get_available_zones()
        if not all_azs:
            return None
        
        available_az = next((az for az in all_azs if az not in existing_azs), all_azs[0])
        
        # Try each VPC CIDR block
        for vpc_cidr in vpc_cidrs:
            vpc_network = ipaddress.ip_network(vpc_cidr)
            
            # Try /24 subnets first, checking last 20 for conflicts
            try:
                all_24_subnets = list(vpc_network.subnets(new_prefix=24))
                if all_24_subnets:
                    # Check last 20 subnets in reverse order
                    for subnet in reversed(all_24_subnets[-20:]):
                        # Check if it conflicts
                        if not any(subnet.overlaps(used) for used in used_networks):
                            print(f"  Suggesting CIDR: {subnet} (verified no conflicts)")
                            return str(subnet), available_az
            except ValueError:
                pass
            
            # Fallback: try smaller subnets
            for size in [25, 26, 27]:
                try:
                    all_subnets = list(vpc_network.subnets(new_prefix=size))
                    if all_subnets:
                        # Check last 10 subnets
                        for subnet in reversed(all_subnets[-10:]):
                            if not any(subnet.overlaps(used) for used in used_networks):
                                print(f"  Suggesting CIDR: {subnet} (verified no conflicts)")
                                return str(subnet), available_az
                except ValueError:
                    continue
        
        print(f"  Could not suggest a non-conflicting CIDR")
        return None
    
    def get_available_zones(self) -> List[str]:
        """Get available availability zones in the region"""
        try:
            response = self.ec2.describe_availability_zones(
                Filters=[{"Name": "state", "Values": ["available"]}]
            )
            return [az["ZoneName"] for az in response["AvailabilityZones"]]
        except ClientError as e:
            print(f"Error getting availability zones: {e}")
            return []
    
    def create_subnet(self, vpc_id: str, cidr_block: str, availability_zone: Optional[str] = None, name: Optional[str] = None) -> Optional[str]:
        """Create a new subnet in the VPC"""
        try:
            subnet_name = name or f"cpu-stresser-subnet-{int(time.time())}"
            print(f"Creating subnet: {subnet_name} ({cidr_block})")
            
            params = {
                "VpcId": vpc_id,
                "CidrBlock": cidr_block,
                "TagSpecifications": [
                    {
                        "ResourceType": "subnet",
                        "Tags": [
                            {"Key": "Name", "Value": subnet_name},
                            {"Key": "Project", "Value": "cpu-stresser"}
                        ]
                    }
                ]
            }
            
            if availability_zone:
                params["AvailabilityZone"] = availability_zone
            
            response = self.ec2.create_subnet(**params)
            subnet_id = response["Subnet"]["SubnetId"]
            print(f"✓ Subnet created: {subnet_id} in {response['Subnet']['AvailabilityZone']}")
            return subnet_id
        except ClientError as e:
            print(f"Error creating subnet: {e}")
            return None
    
    def get_vpc_and_subnets(self):
        """Get VPC ID and subnet IDs from environment variables or user input"""
        vpc_id = VPC_ID.strip() if VPC_ID else None
        subnet_ids_str = SUBNET_IDS.strip() if SUBNET_IDS else None
        
        # If not in env vars, try to get from user input
        if not vpc_id:
            print("\nVPC ID not found in environment variables.")
            print("Please provide VPC ID and subnet IDs.")
            print("You can set them via environment variables:")
            print("  export VPC_ID=vpc-xxxxx")
            print("  export SUBNET_IDS=subnet-xxxxx,subnet-yyyyy")
            print("\nOr provide them now:")
            vpc_id = input("VPC ID: ").strip()
            if not vpc_id:
                print("✗ VPC ID is required")
                return None, []
        
        if not subnet_ids_str:
            subnet_ids_str = input("Subnet IDs (comma-separated): ").strip()
            if not subnet_ids_str:
                print("✗ Subnet IDs are required")
                return None, []
        
        subnet_ids = [s.strip() for s in subnet_ids_str.split(",") if s.strip()]
        if not subnet_ids:
            print("✗ At least one subnet ID is required")
            return None, []
        
        return vpc_id, subnet_ids
    
    def create_network_load_balancer(self, subnets: List[str], vpc_id: Optional[str] = None) -> Optional[str]:
        """Create a Network Load Balancer"""
        try:
            print(f"Creating Network Load Balancer: {NLB_NAME}")
            response = self.elbv2.create_load_balancer(
                Name=NLB_NAME,
                Type="network",
                Subnets=subnets,
                Scheme="internet-facing",
                Tags=[
                    {"Key": "Name", "Value": NLB_NAME},
                    {"Key": "Project", "Value": "cpu-stresser"}
                ]
            )
            nlb_arn = response["LoadBalancers"][0]["LoadBalancerArn"]
            self.final_subnets = subnets  # Store the subnets used
            print(f"✓ Network Load Balancer created: {nlb_arn}")
            return nlb_arn
        except ClientError as e:
            if e.response["Error"]["Code"] == "DuplicateLoadBalancerName":
                print(f"Load balancer {NLB_NAME} already exists, fetching ARN...")
                response = self.elbv2.describe_load_balancers(Names=[NLB_NAME])
                self.final_subnets = subnets
                return response["LoadBalancers"][0]["LoadBalancerArn"]
            elif e.response["Error"]["Code"] == "InvalidSubnet" and vpc_id:
                error_msg = str(e)
                if "Not enough IP space" in error_msg or "at least 8 free IP addresses" in error_msg:
                    print(f"⚠ Subnet has insufficient IP space. Looking for alternative...")
                    
                    # Extract problematic subnet ID from error message
                    problematic_subnet_match = re.search(r'subnet-[a-f0-9]+', error_msg)
                    problematic_subnets = []
                    if problematic_subnet_match:
                        problematic_subnet_id = problematic_subnet_match.group(0)
                        problematic_subnets.append(problematic_subnet_id)
                        print(f"  Identified problematic subnet: {problematic_subnet_id}")
                    
                    # Remove problematic subnets from the list
                    working_subnets = [s for s in subnets if s not in problematic_subnets]
                    if not working_subnets:
                        print(f"  All provided subnets have insufficient IP space")
                    
                    # First, check if any existing subnet in a different AZ has space
                    print(f"  Checking existing subnets for available IP space...")
                    exclude_subnets = list(set(subnets + problematic_subnets))
                    existing_subnet_id = self.check_existing_subnet_space(vpc_id, exclude_subnets)
                    if existing_subnet_id:
                        # Use working subnets + the existing one with space
                        extended_subnets = working_subnets + [existing_subnet_id]
                        print(f"  Using existing subnet with available space")
                        print(f"Retrying NLB creation with {len(extended_subnets)} subnet(s)...")
                        return self.create_network_load_balancer(extended_subnets, vpc_id)
                    
                    # If no existing subnet has space, try to create a new one
                    print(f"  No existing subnets have sufficient space. Creating new subnet...")
                    cidr_az = self.find_available_cidr(vpc_id)
                    if cidr_az:
                        cidr_block, az = cidr_az
                        new_subnet_id = self.create_subnet(vpc_id, cidr_block, az)
                        if new_subnet_id:
                            # Use working subnets + the new one (exclude problematic ones)
                            extended_subnets = working_subnets + [new_subnet_id]
                            print(f"Retrying NLB creation with {len(extended_subnets)} subnet(s) (excluding problematic ones)...")
                            return self.create_network_load_balancer(extended_subnets, vpc_id)
                    else:
                        print(f"✗ Could not find available CIDR block for new subnet")
                        print(f"  Attempting to create subnet with suggested CIDR...")
                        # Try to suggest a CIDR based on VPC structure
                        suggested_cidr = self.suggest_subnet_cidr(vpc_id)
                        if suggested_cidr:
                            cidr_block, az = suggested_cidr
                            new_subnet_id = self.create_subnet(vpc_id, cidr_block, az)
                            if new_subnet_id:
                                extended_subnets = working_subnets + [new_subnet_id]
                                print(f"Retrying NLB creation with {len(extended_subnets)} subnet(s)...")
                                return self.create_network_load_balancer(extended_subnets, vpc_id)
                        else:
                            print(f"✗ Cannot create new subnet - VPC appears to be full")
                            print(f"  Options:")
                            print(f"    1. Add a secondary CIDR block to your VPC")
                            print(f"    2. Delete unused subnets to free up CIDR space")
                            print(f"    3. Manually create a subnet and provide it via SUBNET_IDS")
            print(f"Error creating NLB: {e}")
            return None
    
    def create_target_group(self, vpc_id: str, port: int = 8080) -> Optional[str]:
        """Create a target group for the load balancer"""
        try:
            print(f"Creating Target Group: {TARGET_GROUP_NAME}")
            response = self.elbv2.create_target_group(
                Name=TARGET_GROUP_NAME,
                Protocol="TCP",
                Port=port,
                VpcId=vpc_id,
                TargetType="instance",
                HealthCheckProtocol="TCP",
                HealthCheckPort=str(port),
                HealthCheckEnabled=True,
                Tags=[
                    {"Key": "Name", "Value": TARGET_GROUP_NAME},
                    {"Key": "Project", "Value": "cpu-stresser"}
                ]
            )
            tg_arn = response["TargetGroups"][0]["TargetGroupArn"]
            print(f"✓ Target Group created: {tg_arn}")
            return tg_arn
        except ClientError as e:
            if e.response["Error"]["Code"] == "DuplicateTargetGroupName":
                print(f"Target group {TARGET_GROUP_NAME} already exists, fetching ARN...")
                response = self.elbv2.describe_target_groups(Names=[TARGET_GROUP_NAME])
                return response["TargetGroups"][0]["TargetGroupArn"]
            print(f"Error creating target group: {e}")
            return None
    
    def create_listener(self, load_balancer_arn: str, target_group_arn: str, port: int = 8080):
        """Create a listener for the load balancer"""
        try:
            print(f"Creating listener on port {port}")
            # Check if listener already exists
            response = self.elbv2.describe_listeners(LoadBalancerArn=load_balancer_arn)
            for listener in response.get("Listeners", []):
                if listener["Port"] == port:
                    print(f"✓ Listener on port {port} already exists")
                    return listener["ListenerArn"]
            
            response = self.elbv2.create_listener(
                LoadBalancerArn=load_balancer_arn,
                Protocol="TCP",
                Port=port,
                DefaultActions=[
                    {
                        "Type": "forward",
                        "TargetGroupArn": target_group_arn
                    }
                ]
            )
            listener_arn = response["Listeners"][0]["ListenerArn"]
            print(f"✓ Listener created: {listener_arn}")
            return listener_arn
        except ClientError as e:
            print(f"Error creating listener: {e}")
            return None
    
    def create_auto_scaling_group(
        self,
        target_group_arn: str,
        subnets: List[str],
        min_size: int = 1,
        max_size: int = 5,
        desired_capacity: int = 2
    ) -> bool:
        """Create an Auto Scaling Group using the launch template"""
        try:
            print(f"Creating Auto Scaling Group: {ASG_NAME}")
            
            # Verify launch template exists
            try:
                self.ec2.describe_launch_template_versions(LaunchTemplateId=LAUNCH_TEMPLATE_ID)
                print(f"✓ Launch Template verified: {LAUNCH_TEMPLATE_ID}")
            except ClientError as e:
                print(f"Error: Launch Template {LAUNCH_TEMPLATE_ID} not found: {e}")
                return False
            
            response = self.autoscaling.create_auto_scaling_group(
                AutoScalingGroupName=ASG_NAME,
                LaunchTemplate={
                    "LaunchTemplateId": LAUNCH_TEMPLATE_ID,
                    "Version": "$Latest"
                },
                MinSize=min_size,
                MaxSize=max_size,
                DesiredCapacity=desired_capacity,
                VPCZoneIdentifier=",".join(subnets),
                TargetGroupARNs=[target_group_arn],
                HealthCheckType="ELB",
                HealthCheckGracePeriod=300,
                Tags=[
                    {
                        "Key": "Name",
                        "Value": ASG_NAME,
                        "PropagateAtLaunch": True
                    },
                    {
                        "Key": "Project",
                        "Value": "cpu-stresser",
                        "PropagateAtLaunch": True
                    }
                ]
            )
            print(f"✓ Auto Scaling Group created: {ASG_NAME}")
            return True
        except ClientError as e:
            if e.response["Error"]["Code"] == "AlreadyExists":
                print(f"Auto Scaling Group {ASG_NAME} already exists")
                return True
            print(f"Error creating ASG: {e}")
            return False
    
    def create_target_tracking_policy(
        self,
        asg_name: str,
        target_value: float = 50.0,
        metric_type: str = "ASGAverageCPUUtilization"
    ) -> Optional[str]:
        """Create a target tracking scaling policy for the ASG"""
        try:
            policy_name = f"{asg_name}-target-tracking"
            print(f"Creating Target Tracking Policy: {policy_name}")
            
            # Build the target tracking configuration
            # Note: Cooldowns are managed automatically by target tracking policies
            target_tracking_config = {
                "TargetValue": target_value,
                "PredefinedMetricSpecification": {
                    "PredefinedMetricType": metric_type
                },
                "DisableScaleIn": False  # Enable scale-in
            }
            
            response = self.autoscaling.put_scaling_policy(
                AutoScalingGroupName=asg_name,
                PolicyName=policy_name,
                PolicyType="TargetTrackingScaling",
                TargetTrackingConfiguration=target_tracking_config,
                Enabled=True
            )
            
            policy_arn = response["PolicyARN"]
            print(f"✓ Target Tracking Policy created: {policy_arn}")
            print(f"  Target: {target_value}% {metric_type}")
            print(f"  Scale-in: Enabled (cooldowns managed automatically)")
            return policy_arn
        except ClientError as e:
            if e.response["Error"]["Code"] == "AlreadyExists":
                print(f"Policy {policy_name} already exists, updating...")
                # Try to update by deleting and recreating
                try:
                    self.autoscaling.delete_policy(
                        AutoScalingGroupName=asg_name,
                        PolicyName=policy_name
                    )
                    time.sleep(2)  # Brief wait before recreating
                    return self.create_target_tracking_policy(
                        asg_name, target_value, metric_type
                    )
                except ClientError:
                    pass
            print(f"Error creating target tracking policy: {e}")
            return None
    
    def set_asg_instance_warmup(self, asg_name: str, warmup_seconds: int = 60) -> bool:
        """Set the instance warmup period for the ASG"""
        try:
            print(f"Setting ASG instance warmup to {warmup_seconds} seconds...")
            self.autoscaling.update_auto_scaling_group(
                AutoScalingGroupName=asg_name,
                DefaultInstanceWarmup=warmup_seconds
            )
            print(f"✓ Instance warmup set to {warmup_seconds} seconds")
            return True
        except ClientError as e:
            print(f"Error setting instance warmup: {e}")
            return False
    
    def set_asg_default_cooldown(self, asg_name: str, cooldown_seconds: int = 300) -> bool:
        """Set the default cooldown period for the ASG"""
        try:
            print(f"Setting ASG default cooldown to {cooldown_seconds} seconds...")
            self.autoscaling.update_auto_scaling_group(
                AutoScalingGroupName=asg_name,
                DefaultCooldown=cooldown_seconds
            )
            print(f"✓ Default cooldown set to {cooldown_seconds} seconds")
            return True
        except ClientError as e:
            print(f"Error setting default cooldown: {e}")
            return False
    
    def wait_for_nlb_active(self, load_balancer_arn: str, timeout: int = 300):
        """Wait for the load balancer to become active"""
        print("Waiting for Network Load Balancer to become active...")
        start_time = time.time()
        while time.time() - start_time < timeout:
            try:
                response = self.elbv2.describe_load_balancers(
                    LoadBalancerArns=[load_balancer_arn]
                )
                state = response["LoadBalancers"][0]["State"]["Code"]
                if state == "active":
                    print("✓ Network Load Balancer is active")
                    return True
                print(f"  NLB state: {state}")
                time.sleep(10)
            except ClientError as e:
                print(f"Error checking NLB status: {e}")
                return False
        print("Timeout waiting for NLB to become active")
        return False
    
    def get_load_balancer_dns(self, load_balancer_arn: str) -> Optional[str]:
        """Get the DNS name of the load balancer"""
        try:
            response = self.elbv2.describe_load_balancers(
                LoadBalancerArns=[load_balancer_arn]
            )
            return response["LoadBalancers"][0]["DNSName"]
        except ClientError as e:
            print(f"Error getting NLB DNS: {e}")
            return None
    
    def deploy(self, min_size: int = 1, max_size: int = 5, desired_capacity: int = 2):
        """Deploy the complete infrastructure"""
        policy_arn = None  # Initialize scaling policy ARN
        print("=" * 60)
        print("Deploying CPU Stresser Infrastructure")
        print("=" * 60)
        print(f"Region: {self.region}")
        print(f"AMI ID: {AMI_ID}")
        print(f"Launch Template ID: {LAUNCH_TEMPLATE_ID}")
        print()
        
        # Get VPC and subnets
        print("Step 1: Getting VPC and subnet information...")
        vpc_id, subnets = self.get_vpc_and_subnets()
        if not vpc_id or not subnets:
            print("✗ Failed to get VPC and subnets")
            return False
        print(f"✓ Using VPC: {vpc_id}")
        print(f"✓ Using subnets: {subnets}")
        print()
        
        # Create Network Load Balancer
        print("Step 2: Creating Network Load Balancer...")
        nlb_arn = self.create_network_load_balancer(subnets, vpc_id)
        if not nlb_arn:
            print("✗ Failed to create NLB")
            return False
        print()
        
        # Wait for NLB to be active
        if not self.wait_for_nlb_active(nlb_arn):
            print("✗ NLB did not become active in time")
            return False
        print()
        
        # Create Target Group
        print("Step 3: Creating Target Group...")
        tg_arn = self.create_target_group(vpc_id)
        if not tg_arn:
            print("✗ Failed to create target group")
            return False
        print()
        
        # Create Listener
        print("Step 4: Creating Listener...")
        listener_arn = self.create_listener(nlb_arn, tg_arn)
        if not listener_arn:
            print("✗ Failed to create listener")
            return False
        print()
        
        # Create Auto Scaling Group
        print("Step 5: Creating Auto Scaling Group...")
        # Use final_subnets if available (includes any newly created subnet), otherwise use original subnets
        asg_subnets = self.final_subnets if self.final_subnets else subnets
        asg_success = self.create_auto_scaling_group(
            tg_arn,
            asg_subnets,
            min_size=min_size,
            max_size=max_size,
            desired_capacity=desired_capacity
        )
        if not asg_success:
            print("✗ Failed to create ASG")
            return False
        print()
        
        # Set instance warmup period and default cooldown
        print("Step 6: Configuring ASG settings...")
        self.set_asg_instance_warmup(ASG_NAME, warmup_seconds=60)
        self.set_asg_default_cooldown(ASG_NAME, cooldown_seconds=300)
        print()
        
        # Create Target Tracking Scaling Policy
        print("Step 7: Creating Dynamic Scaling Policy...")
        policy_arn = self.create_target_tracking_policy(
            ASG_NAME,
            target_value=50.0,
            metric_type="ASGAverageCPUUtilization"
        )
        if not policy_arn:
            print("⚠ Failed to create scaling policy (ASG will still work with manual scaling)")
        print()
        
        # Get NLB DNS name
        dns_name = self.get_load_balancer_dns(nlb_arn)
        
        print("=" * 60)
        print("Deployment Complete!")
        print("=" * 60)
        print(f"Network Load Balancer ARN: {nlb_arn}")
        if dns_name:
            print(f"Network Load Balancer DNS: {dns_name}")
        print(f"Target Group ARN: {tg_arn}")
        print(f"Auto Scaling Group: {ASG_NAME}")
        if policy_arn:
            print(f"Scaling Policy: {policy_arn}")
            print(f"  - Target: 50% CPU utilization")
            print(f"  - Instance warmup: 60 seconds")
            print(f"  - Default cooldown: 300 seconds")
            print(f"  - Scale-in: Enabled (cooldowns managed automatically by target tracking)")
        print()
        print("Resources are being provisioned. Instances will be launched shortly.")
        print("The ASG will automatically scale based on CPU utilization.")
        return True


def main():
    """Main entry point"""
    import sys
    
    # Verify AWS credentials are set
    if not all([
        os.getenv("AWS_ACCESS_KEY_ID"),
        os.getenv("AWS_SECRET_ACCESS_KEY"),
        os.getenv("AWS_SESSION_TOKEN")
    ]):
        print("Error: AWS credentials not found in environment variables")
        print("Please ensure AWS_ACCESS_KEY_ID, AWS_SECRET_ACCESS_KEY, and AWS_SESSION_TOKEN are set")
        print("\nYou can source your .env file: source .env")
        return
    
    # Check if VPC_ID and SUBNET_IDS are provided via command line
    if len(sys.argv) >= 3:
        os.environ["VPC_ID"] = sys.argv[1]
        os.environ["SUBNET_IDS"] = sys.argv[2]
        print(f"Using VPC ID from command line: {sys.argv[1]}")
        print(f"Using Subnet IDs from command line: {sys.argv[2]}")
    
    infra = AWSInfrastructure()
    success = infra.deploy(
        min_size=1,
        max_size=5,
        desired_capacity=2
    )
    
    if success:
        print("\n✓ Infrastructure deployment completed successfully")
    else:
        print("\n✗ Infrastructure deployment failed")
        exit(1)


if __name__ == "__main__":
    main()
